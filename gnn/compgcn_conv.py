import torch
from torch.nn import Parameter
from torch_scatter import scatter_add

from gnn.message_passing import MessagePassing
from helper import (
	get_param,
	ccorr
)


class CompGCNConv(MessagePassing):
	def __init__(
		self,
		in_channels,
		out_channels,
		use_bias=True,
		use_bn=True,
		opn='sub',
		dropout=0
		):
		super(self.__class__, self).__init__()

		self.in_channels	= in_channels
		self.out_channels	= out_channels
		self.use_bias 		= use_bias
		self.use_bn 		= use_bn
		self.opn 			= opn
		self.dropout 		= dropout
		self.device			= None

		self.w_loop			= get_param((in_channels, out_channels))
		self.w_in			= get_param((in_channels, out_channels))
		self.w_out			= get_param((in_channels, out_channels))
		self.w_rel 			= get_param((in_channels, out_channels))
		self.loop_rel 		= get_param((1, in_channels))

		self.drop			= torch.nn.Dropout(self.dropout)

		if self.use_bias: 
			self.register_parameter('bias', Parameter(torch.zeros(out_channels)))
		
		if self.use_bn:
			self.bn = torch.nn.BatchNorm1d(out_channels)


	def forward(self, ent_embed, rel_embed, edge_index, edge_type, ent_mask=None):
		"""
		Message passing layer.

		Args:
			ent_embed (Tensor): Shape: (num_nodes, embed_dim)
				The entity embeddings.
			rel_embed (Tensor): Shape: (2 * num_edges, embed_dim)
				The edges embeddings. This includes the edges representation for inverse edges.
			edge_index (Tensor): Shape (2, num_edges)
				The edge index, pairs of source/target nodes. This does not include inverse edges, these
				are created locally and can be inferred from the original edge indices.
			edge_type (Tensor): Shape (num_edges,)
				The edge type (batch-local edge ID) for each edge. This does not include inverse edges,
				these can be inferred locally by shifting the IDs by the number of original edges.

		Returns:
			Tensor: Shape (num_nodes, embed_dim)
				Updated node embeddings.
		"""
		if self.device is None:
			self.device = edge_index.device

		rel_embed 	= torch.cat([rel_embed, self.loop_rel], dim=0) # contains in, out and loop
		num_edges 	= edge_index.size(1)
		edge_index	= torch.cat([edge_index, edge_index.flip(0)], dim=1) # add inverse relations to edge index
		num_ent   	= ent_embed.size(0)

		self.in_index 	= edge_index[:, :num_edges]				# indices of normal edges
		self.out_index 	= edge_index[:, num_edges:]				# indices of inversed edges
		self.in_type 	= edge_type								# original types
		self.out_type	= edge_type + rel_embed.shape[0] // 2	# original types shifted by length of relation embeddings

		self.loop_index	= torch.stack([torch.arange(num_ent), torch.arange(num_ent)]).to(self.device)
		self.loop_type  = torch.full((num_ent,), rel_embed.size(0)-1, dtype=torch.long).to(self.device)

		self.in_norm    = self.compute_norm(self.in_index,  num_ent)
		self.out_norm   = self.compute_norm(self.out_index, num_ent)
		
		in_res		= self.propagate('add', self.in_index,   x=ent_embed, edge_type=self.in_type,   rel_embed=rel_embed, edge_norm=self.in_norm, 	mode='in')
		loop_res	= self.propagate('add', self.loop_index, x=ent_embed, edge_type=self.loop_type, rel_embed=rel_embed, edge_norm=None, 			mode='loop')
		out_res		= self.propagate('add', self.out_index,  x=ent_embed, edge_type=self.out_type,  rel_embed=rel_embed, edge_norm=self.out_norm,	mode='out')
		out			= self.drop(in_res)*(1/3) + self.drop(out_res)*(1/3) + loop_res*(1/3)

		if self.use_bias: 
			out = out + self.bias
		if self.use_bn:
			out = self.bn(out)

		if ent_mask is not None:
			out[ent_mask] = ent_embed[ent_mask]

		return out, torch.matmul(rel_embed, self.w_rel)[:-1]	# Ignoring the self loop inserted, which is the last index

	def rel_transform(self, ent_embed, rel_embed):
		if   self.opn == 'corr': 	trans_embed  = ccorr(ent_embed, rel_embed)
		elif self.opn == 'sub': 	trans_embed  = ent_embed - rel_embed
		elif self.opn == 'mult': 	trans_embed  = ent_embed * rel_embed
		else: raise NotImplementedError

		return trans_embed

	def message(self, x_j, edge_type, rel_embed, edge_norm, mode):
		weight 	= getattr(self, 'w_{}'.format(mode))
		rel_emb = torch.index_select(rel_embed, 0, edge_type)
		xj_rel  = self.rel_transform(x_j, rel_emb)
		out	= torch.mm(xj_rel, weight)

		return out if edge_norm is None else out * edge_norm.view(-1, 1)

	def update(self, aggr_out):
		return aggr_out

	def compute_norm(self, edge_index, num_ent):
		row, col	= edge_index
		edge_weight 	= torch.ones_like(row).float()
		deg		= scatter_add( edge_weight, row, dim=0, dim_size=num_ent)	# Summing number of weights of the edges
		deg_inv		= deg.pow(-0.5)							# D^{-0.5}
		deg_inv[deg_inv	== float('inf')] = 0
		norm		= deg_inv[row] * edge_weight * deg_inv[col]			# D^{-0.5}

		return norm

	def __repr__(self):
		return '{}({}, {}, num_rels={})'.format(
			self.__class__.__name__, self.in_channels, self.out_channels, self.num_rels)