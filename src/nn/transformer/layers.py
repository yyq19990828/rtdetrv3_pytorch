"""
Transformer layers for RT-DETRv3
"""

import math
from typing import Optional, List

import torch
import torch.nn as nn
import torch.nn.functional as F


class MLP(nn.Module):
    """Multi-layer perceptron"""
    
    def __init__(self, input_dim: int, hidden_dim: int, output_dim: int, num_layers: int):
        super().__init__()
        self.num_layers = num_layers
        
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(
            nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim])
        )
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


class MultiHeadAttention(nn.Module):
    """Multi-head self-attention"""
    
    def __init__(self, d_model: int, nhead: int, dropout: float = 0.0):
        super().__init__()
        self.d_model = d_model
        self.nhead = nhead
        self.head_dim = d_model // nhead
        
        assert self.head_dim * nhead == d_model, "d_model must be divisible by nhead"
        
        self.q_proj = nn.Linear(d_model, d_model)
        self.k_proj = nn.Linear(d_model, d_model)
        self.v_proj = nn.Linear(d_model, d_model)
        self.out_proj = nn.Linear(d_model, d_model)
        
        self.dropout = nn.Dropout(dropout)
        
        self._reset_parameters()
    
    def _reset_parameters(self):
        """Initialize parameters"""
        nn.init.xavier_uniform_(self.q_proj.weight)
        nn.init.xavier_uniform_(self.k_proj.weight)
        nn.init.xavier_uniform_(self.v_proj.weight)
        nn.init.xavier_uniform_(self.out_proj.weight)
        
        nn.init.constant_(self.q_proj.bias, 0)
        nn.init.constant_(self.k_proj.bias, 0)
        nn.init.constant_(self.v_proj.bias, 0)
        nn.init.constant_(self.out_proj.bias, 0)
    
    def forward(self,
                query: torch.Tensor,
                key: torch.Tensor,
                value: torch.Tensor,
                attn_mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """Forward pass"""
        batch_size, seq_len, _ = query.shape
        
        # Linear projections
        q = self.q_proj(query).view(batch_size, seq_len, self.nhead, self.head_dim).transpose(1, 2)
        k = self.k_proj(key).view(batch_size, seq_len, self.nhead, self.head_dim).transpose(1, 2)
        v = self.v_proj(value).view(batch_size, seq_len, self.nhead, self.head_dim).transpose(1, 2)
        
        # Scaled dot-product attention
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.head_dim)
        
        if attn_mask is not None:
            scores = scores + attn_mask
        
        attn_weights = F.softmax(scores, dim=-1)
        attn_weights = self.dropout(attn_weights)
        
        # Apply attention to values
        attn_output = torch.matmul(attn_weights, v)
        
        # Concatenate heads
        attn_output = attn_output.transpose(1, 2).contiguous().view(
            batch_size, seq_len, self.d_model
        )
        
        # Final linear projection
        output = self.out_proj(attn_output)
        
        return output


class MSDeformableAttention(nn.Module):
    """Multi-Scale Deformable Attention"""
    
    def __init__(self,
                 d_model: int = 256,
                 n_heads: int = 8,
                 n_levels: int = 4,
                 n_points: int = 4,
                 dropout: float = 0.0,
                 ratio: float = 1.0):
        super().__init__()
        
        if d_model % n_heads != 0:
            raise ValueError(f"d_model must be divisible by n_heads, got {d_model} and {n_heads}")
        
        self.d_model = d_model
        self.n_heads = n_heads
        self.n_levels = n_levels
        self.n_points = n_points
        self.head_dim = d_model // n_heads
        
        self.sampling_offsets = nn.Linear(d_model, n_heads * n_levels * n_points * 2)
        self.attention_weights = nn.Linear(d_model, n_heads * n_levels * n_points)
        self.value_proj = nn.Linear(d_model, int(d_model * ratio))
        self.output_proj = nn.Linear(int(d_model * ratio), d_model)
        
        self.dropout = nn.Dropout(dropout)
        
        self._reset_parameters()
    
    def _reset_parameters(self):
        """Initialize parameters"""
        # Initialize sampling offsets
        nn.init.constant_(self.sampling_offsets.weight, 0)
        
        # Initialize sampling offsets with a pattern
        thetas = torch.arange(self.n_heads, dtype=torch.float32) * (2.0 * math.pi / self.n_heads)
        grid_init = torch.stack([thetas.cos(), thetas.sin()], -1)
        grid_init = (grid_init / grid_init.abs().max(-1, keepdim=True)[0]).view(
            self.n_heads, 1, 1, 2).repeat(1, self.n_levels, self.n_points, 1)
        
        for i in range(self.n_points):
            grid_init[:, :, i, :] *= i + 1
        
        with torch.no_grad():
            self.sampling_offsets.bias = nn.Parameter(grid_init.view(-1))
        
        # Initialize attention weights
        nn.init.constant_(self.attention_weights.weight, 0)
        nn.init.constant_(self.attention_weights.bias, 0)
        
        # Initialize value and output projections
        nn.init.xavier_uniform_(self.value_proj.weight)
        nn.init.constant_(self.value_proj.bias, 0)
        nn.init.xavier_uniform_(self.output_proj.weight)
        nn.init.constant_(self.output_proj.bias, 0)
    
    def forward(self,
                query: torch.Tensor,
                reference_points: torch.Tensor,
                value: torch.Tensor,
                value_spatial_shapes: torch.Tensor,
                value_level_start_index: torch.Tensor,
                value_mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """Forward pass"""
        bs, len_q, _ = query.shape
        len_v = value.shape[1]
        
        # Value projection
        value = self.value_proj(value)
        if value_mask is not None:
            value = value * value_mask.unsqueeze(-1)
        
        value = value.view(bs, len_v, self.n_heads, -1)
        
        # Sampling offsets and attention weights
        sampling_offsets = self.sampling_offsets(query).view(
            bs, len_q, self.n_heads, self.n_levels, self.n_points, 2
        )
        
        attention_weights = self.attention_weights(query).view(
            bs, len_q, self.n_heads, self.n_levels * self.n_points
        )
        attention_weights = F.softmax(attention_weights, dim=-1).view(
            bs, len_q, self.n_heads, self.n_levels, self.n_points
        )
        
        # Compute sampling locations
        if reference_points.shape[-1] == 2:
            offset_normalizer = value_spatial_shapes.flip([1]).view(1, 1, 1, self.n_levels, 1, 2)
            sampling_locations = reference_points.view(
                bs, len_q, 1, self.n_levels, 1, 2
            ) + sampling_offsets / offset_normalizer
        elif reference_points.shape[-1] == 4:
            sampling_locations = (
                reference_points[:, :, None, :, None, :2] + 
                sampling_offsets / self.n_points * reference_points[:, :, None, :, None, 2:] * 0.5
            )
        else:
            raise ValueError(f"Last dim of reference_points must be 2 or 4, got {reference_points.shape[-1]}")
        
        # Apply deformable attention
        try:
            # Try to use the optimized implementation if available
            from torchvision.ops import deform_conv2d
            output = self._deformable_attention_core_func(
                value, value_spatial_shapes, value_level_start_index,
                sampling_locations, attention_weights
            )
        except ImportError:
            # Fallback to the default implementation
            output = self._deformable_attention_core_func(
                value, value_spatial_shapes, value_level_start_index,
                sampling_locations, attention_weights
            )
        
        output = self.output_proj(output)
        
        return output
    
    def _deformable_attention_core_func(self,
                                       value: torch.Tensor,
                                       value_spatial_shapes: torch.Tensor,
                                       value_level_start_index: torch.Tensor,
                                       sampling_locations: torch.Tensor,
                                       attention_weights: torch.Tensor) -> torch.Tensor:
        """Core deformable attention function"""
        bs, _, n_heads, c = value.shape
        _, len_q, _, n_levels, n_points, _ = sampling_locations.shape
        
        # Split value by levels
        value_list = value.split([H * W for H, W in value_spatial_shapes], dim=1)
        sampling_grids = 2 * sampling_locations - 1
        sampling_value_list = []
        
        for level, (H, W) in enumerate(value_spatial_shapes):
            # Reshape value: [bs, H*W, n_heads, c] -> [bs*n_heads, c, H, W]
            value_l = value_list[level].flatten(2).transpose(1, 2).reshape(bs * n_heads, c, H, W)
            
            # Reshape sampling grid: [bs, len_q, n_heads, n_points, 2] -> [bs*n_heads, len_q, n_points, 2]
            sampling_grid_l = sampling_grids[:, :, :, level].transpose(1, 2).flatten(0, 1)
            
            # Grid sample
            sampling_value_l = F.grid_sample(
                value_l, sampling_grid_l,
                mode='bilinear', padding_mode='zeros', align_corners=False
            )
            sampling_value_list.append(sampling_value_l)
        
        # Weighted sum
        attention_weights = attention_weights.transpose(1, 2).reshape(
            bs * n_heads, 1, len_q, n_levels * n_points
        )
        
        output = torch.stack(sampling_value_list, dim=-1).flatten(-2) * attention_weights
        output = output.sum(-1).view(bs, n_heads * c, len_q)
        
        return output.transpose(1, 2)


class PositionEmbedding(nn.Module):
    """Position embedding for transformers"""
    
    def __init__(self, d_model: int, max_len: int = 5000):
        super().__init__()
        
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        
        self.register_buffer('pe', pe.unsqueeze(0))
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass"""
        return x + self.pe[:, :x.size(1)]