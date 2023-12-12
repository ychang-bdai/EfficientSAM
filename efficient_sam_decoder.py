# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from typing import List, Tuple, Type

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from mlp import MLPBlock


class PromptEncoder(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        image_embedding_size: Tuple[int, int],
        input_image_size: Tuple[int, int],
    ) -> None:
        """
        Encodes prompts for input to SAM's mask decoder.

        Arguments:
          embed_dim (int): The prompts' embedding dimension
          image_embedding_size (tuple(int, int)): The spatial size of the
            image embedding, as (H, W).
          input_image_size (int): The padded size of the image as input
            to the image encoder, as (H, W).
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.input_image_size = input_image_size
        self.image_embedding_size = image_embedding_size
        self.pe_layer = PositionEmbeddingRandom(embed_dim // 2)
        self.invalid_points = nn.Embedding(1, embed_dim)
        self.point_embeddings = nn.Embedding(1, embed_dim)
        self.bbox_top_left_embeddings = nn.Embedding(1, embed_dim)
        self.bbox_bottom_right_embeddings = nn.Embedding(1, embed_dim)

    def get_dense_pe(self) -> torch.Tensor:
        """
        Returns the positional encoding used to encode point prompts,
        applied to a dense set of points the shape of the image encoding.

        Returns:
          torch.Tensor: Positional encoding with shape
            1x(embed_dim)x(embedding_h)x(embedding_w)
        """
        return self.pe_layer(self.image_embedding_size).unsqueeze(0)

    def _embed_points(
        self,
        points: torch.Tensor,
        labels: torch.Tensor,
    ) -> torch.Tensor:
        """Embeds point prompts."""
        points = points + 0.5  # Shift to center of pixel
        point_embedding = self.pe_layer.forward_with_coords(
            points, self.input_image_size
        )
        point_embedding[labels == -1] += self.invalid_points.weight
        point_embedding[labels == 1] += self.point_embeddings.weight
        point_embedding[labels == 2] += self.bbox_top_left_embeddings.weight
        point_embedding[labels == 3] += self.bbox_bottom_right_embeddings.weight
        return point_embedding

    def _get_device(self) -> torch.device:
        return self.point_embeddings.weight.device

    def forward(
        self,
        coords,
        labels,
    ) -> torch.Tensor:
        """
        Embeds different types of prompts, returning both sparse and dense
        embeddings.

        Arguments:
          points: A tensor of shape [B, 2]
          labels: An integer tensor of shape [B] where each element is 1,2 or 3.

        Returns:
          torch.Tensor: sparse embeddings for the points and boxes, with shape
            BxNx(embed_dim), where N is determined by the number of input points
            and boxes.
        """
        return self._embed_points(coords, labels)


class NullPromptEncoder(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        image_embedding_size: Tuple[int, int],
    ) -> None:
        """
        Null encoder that only returns zeros for all inputs. This is useful when the prompts are already encoded earlier in the network.

        Arguments:
          embed_dim (int): The prompts' embedding dimension
          image_embedding_size (tuple(int, int)): The spatial size of the
            image embedding, as (H, W).
          input_image_size (int): The padded size of the image as input
            to the image encoder, as (H, W).
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.image_embedding_size = image_embedding_size
        self.register_buffer(
            "all_zeros", torch.zeros(1, self.embed_dim, *self.image_embedding_size)
        )

    def get_dense_pe(self) -> torch.Tensor:
        """
        Returns the positional encoding used to encode point prompts,
        applied to a dense set of points the shape of the image encoding.

        Returns:
          torch.Tensor: Positional encoding with shape
            1x(embed_dim)x(embedding_h)x(embedding_w)
        """
        return self.all_zeros

    def forward(
        self,
        coords,
        labels,
    ) -> torch.Tensor:
        batch_size, _, _ = coords.shape
        return torch.zeros(batch_size, 1, self.embed_dim, device=coords.device)


class PositionEmbeddingRandom(nn.Module):
    """
    Positional encoding using random spatial frequencies.
    """

    def __init__(self, num_pos_feats: int) -> None:
        super().__init__()
        self.register_buffer(
            "positional_encoding_gaussian_matrix", torch.randn((2, num_pos_feats))
        )

    def _pe_encoding(self, coords: torch.Tensor) -> torch.Tensor:
        """Positionally encode points that are normalized to [0,1]."""
        # assuming coords are in [0, 1]^2 square and have d_1 x ... x d_n x 2 shape
        coords = 2 * coords - 1
        coords = coords @ self.positional_encoding_gaussian_matrix
        coords = 2 * np.pi * coords
        # outputs d_1 x ... x d_n x C shape
        return torch.cat([torch.sin(coords), torch.cos(coords)], dim=-1)

    def forward(self, size: Tuple[int, int]) -> torch.Tensor:
        """Generate positional encoding for a grid of the specified size."""
        h, w = size
        device = self.positional_encoding_gaussian_matrix.device
        grid = torch.ones([h, w], device=device, dtype=torch.float32)
        y_embed = grid.cumsum(dim=0) - 0.5
        x_embed = grid.cumsum(dim=1) - 0.5
        y_embed = y_embed / h
        x_embed = x_embed / w

        pe = self._pe_encoding(torch.stack([x_embed, y_embed], dim=-1))
        return pe.permute(2, 0, 1)  # C x H x W

    def forward_with_coords(
        self, coords_input: torch.Tensor, image_size: Tuple[int, int]
    ) -> torch.Tensor:
        """Positionally encode points that are not normalized to [0,1]."""
        coords = coords_input.clone()
        coords[:, :, 0] = coords[:, :, 0] / image_size[1]
        coords[:, :, 1] = coords[:, :, 1] / image_size[0]
        return self._pe_encoding(coords.to(torch.float))  # B x N x C


class Conv2dWithActivation(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int,
        act_layer: Type[nn.Module],
        normalization_type: str,
    ):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(
                in_channels, out_channels, kernel_size, padding=(kernel_size - 1) // 2
            ),
            act_layer(),
        )
        self.dequant_input_normalization = torch.ao.quantization.DeQuantStub()
        assert (
            normalization_type == "layer_norm"
            or normalization_type == "batch_norm"
            or normalization_type == "sync_batch_norm"
            or normalization_type == "none"
        )
        if normalization_type == "layer_norm":
            self.norm = torch.nn.GroupNorm(1, out_channels)
        elif normalization_type == "batch_norm":
            self.norm = torch.nn.BatchNorm2d(out_channels)
        elif normalization_type == "sync_batch_norm":
            self.norm = torch.nn.SyncBatchNorm(out_channels)
        elif normalization_type == "none":
            self.norm = torch.nn.Identity()
        else:
            raise ValueError(f"normalization type {normalization_type} not supported")
        self.norm.qconfig = None
        self.quant_output_normalization = torch.ao.quantization.QuantStub()

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.conv(x)
        x = self.dequant_input_normalization(x)
        x = self.norm(x)
        x = self.quant_output_normalization(x)
        return x


class DoubleConv(nn.Module):
    """(convolution => [GELU] => LN) * 2"""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        act_layer: Type[nn.Module],
        normalization_type: str,
        normalize_before_activation: bool,
    ):
        super().__init__()
        self.double_conv = nn.Sequential(
            Conv2dWithActivation(
                in_channels,
                out_channels,
                kernel_size=3,
                act_layer=act_layer,
                normalization_type=normalization_type,
            ),
            Conv2dWithActivation(
                out_channels,
                out_channels,
                kernel_size=3,
                act_layer=act_layer,
                normalization_type=normalization_type,
            ),
        )

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.double_conv(x)


class ConvTranspose2dUsingLinear(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, normalization_type: str):
        super().__init__()
        self.out_channels = out_channels

        self.upsampler_linear = nn.Sequential(
            torch.nn.Linear(in_channels, 4 * out_channels, bias=True), torch.nn.ReLU()
        )
        if normalization_type == "layer_norm":
            self.norm = torch.nn.GroupNorm(1, 4 * out_channels)
        elif normalization_type == "batch_norm":
            self.norm = torch.nn.BatchNorm2d(4 * out_channels)
        elif normalization_type == "sync_batch_norm":
            self.norm = torch.nn.SyncBatchNorm(4 * out_channels)
        else:
            raise ValueError(f"normalization type {normalization_type} not supported")
        self.dequant_input_normalization = torch.ao.quantization.DeQuantStub()
        self.norm.qconfig = None
        self.quant_output_normalization = torch.ao.quantization.QuantStub()

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch_size, in_channels, h, w = x.shape
        x = torch.permute(x, (0, 2, 3, 1))
        x = torch.reshape(x, [batch_size * h * w, in_channels])
        x = self.upsampler_linear(x)
        x = torch.reshape(x, [batch_size, h, w, 4 * self.out_channels])
        x = torch.permute(x, (0, 3, 1, 2))
        x = self.dequant_input_normalization(x)
        x = self.norm(x)
        x = self.quant_output_normalization(x)
        x = torch.reshape(x, [batch_size, self.out_channels, 2, 2, h, w])
        x = torch.permute(x, [0, 1, 4, 2, 5, 3])
        return torch.reshape(x, [batch_size, self.out_channels, h * 2, w * 2])


class Down(nn.Module):
    """Downscaling with maxpool then double conv"""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        act_layer: Type[nn.Module],
        normalization_type: str,
        normalize_before_activation: bool,
    ):
        super().__init__()
        self.maxpool_conv = nn.Sequential(
            nn.MaxPool2d(2),
            DoubleConv(
                in_channels,
                out_channels,
                act_layer,
                normalization_type,
                normalize_before_activation,
            ),
        )

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.maxpool_conv(x)


class Up(nn.Module):
    """Upscaling then double conv"""

    def __init__(
        self,
        x1_in_channels: int,
        x2_in_channels: int,
        out_channels: int,
        act_layer: Type[nn.Module],
        normalization_type: str,
        normalize_before_activation: bool,
    ):
        super().__init__()
        self.up = ConvTranspose2dUsingLinear(
            x1_in_channels, x1_in_channels // 2, normalization_type
        )
        self.conv = DoubleConv(
            x1_in_channels // 2 + x2_in_channels,
            out_channels,
            act_layer,
            normalization_type,
            normalize_before_activation,
        )

        # Quant/Dequant stubs
        self.quant_input_x1 = torch.ao.quantization.QuantStub()
        self.quant_input_x2 = torch.ao.quantization.QuantStub()
        self.dequant_output = torch.ao.quantization.DeQuantStub()

    @torch.jit.export
    def forward(self, x1: torch.Tensor, x2: torch.Tensor) -> torch.Tensor:
        x1 = self.quant_input_x1(x1)
        x2 = self.quant_input_x2(x2)

        x1 = self.up(x1)
        # input is CHW
        diffY = x2.size()[2] - x1.size()[2]
        diffX = x2.size()[3] - x1.size()[3]

        x1 = F.pad(x1, [diffX // 2, diffX - diffX // 2, diffY // 2, diffY - diffY // 2])
        x = torch.cat([x2, x1], dim=1)
        return self.dequant_output(self.conv(x))


class MaskDecoder(nn.Module):
    def __init__(
        self,
        *,
        transformer_dim: int,
        transformer: nn.Module,
        num_multimask_outputs: int,
        activation: Type[nn.Module],
        normalization_type: str,
        normalize_before_activation: bool,
        iou_head_depth: int,
        iou_head_hidden_dim: int,
        upscaling_layer_dims: List[int],
        share_hypernetwork_mlp_weights: bool,
    ) -> None:
        """
        Predicts masks given an image and prompt embeddings, using a
        transformer architecture.

        Arguments:
          transformer_dim (int): the channel dimension of the transformer
          transformer (nn.Module): the transformer used to predict masks
          num_multimask_outputs (int): the number of masks to predict
            when disambiguating masks
          activation (nn.Module): the type of activation to use when
            upscaling masks
          iou_head_depth (int): the depth of the MLP used to predict
            mask quality
          iou_head_hidden_dim (int): the hidden dimension of the MLP
            used to predict mask quality
        """
        super().__init__()
        self.transformer_dim = transformer_dim
        self.transformer = transformer

        self.num_multimask_outputs = num_multimask_outputs

        self.iou_token = nn.Embedding(1, transformer_dim)
        if num_multimask_outputs > 1:
            self.num_mask_tokens = num_multimask_outputs + 1
        else:
            self.num_mask_tokens = 1
        self.mask_tokens = nn.Embedding(self.num_mask_tokens, transformer_dim)
        output_dim_after_upscaling = transformer_dim


        self.final_output_upscaling_layers = nn.ModuleList([])
        for idx, layer_dims in enumerate(upscaling_layer_dims):
            self.final_output_upscaling_layers.append(
                nn.Sequential(
                    nn.ConvTranspose2d(
                        output_dim_after_upscaling,
                        layer_dims,
                        kernel_size=2,
                        stride=2,
                    ),
                    nn.GroupNorm(1, layer_dims)
                    if idx < len(upscaling_layer_dims) - 1
                    else nn.Identity(),
                    activation(),
                )
            )
            output_dim_after_upscaling = layer_dims
        if share_hypernetwork_mlp_weights:
            mlp_block = MLPBlock(
                input_dim=transformer_dim,
                hidden_dim=transformer_dim,
                output_dim=output_dim_after_upscaling,
                num_layers=2,
                act=activation,
            )
            self.output_hypernetworks_mlps = nn.ModuleList(
                [mlp_block for i in range(self.num_mask_tokens)]
            )
        else:
            self.output_hypernetworks_mlps = nn.ModuleList(
                [
                    MLPBlock(
                        input_dim=transformer_dim,
                        hidden_dim=transformer_dim,
                        output_dim=output_dim_after_upscaling,
                        num_layers=2,
                        act=activation,
                    )
                    for i in range(self.num_mask_tokens)
                ]
            )

        self.iou_prediction_head = MLPBlock(
            input_dim=transformer_dim,
            hidden_dim=iou_head_hidden_dim,
            output_dim=self.num_mask_tokens,
            num_layers=iou_head_depth,
            act=activation,
        )
        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(
        self,
        image_embeddings: List[torch.Tensor],
        image_pe: torch.Tensor,
        sparse_prompt_embeddings: torch.Tensor,
        multimask_output: bool,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Predict masks given image and prompt embeddings.

        Arguments:
          image_embeddings: A tensor of shape [B, C, H, W] or [B*max_num_queries, C, H, W]
          image_pe (torch.Tensor): positional encoding with the shape of image_embeddings (the batch dimension is broadcastable).
          sparse_prompt_embeddings (torch.Tensor): the embeddings of the points and boxes
          multimask_output (bool): Whether to return multiple masks or a single
            mask.

        Returns:
          torch.Tensor: batched predicted masks
          torch.Tensor: batched predictions of mask quality
        """

        (
            batch_size,
            max_num_queries,
            sparse_embed_dim_1,
            sparse_embed_dim_2,
        ) = sparse_prompt_embeddings.shape
        image_embeddings_tiled = []
        for image_embedding in image_embeddings:
            (
                _,
                image_embed_dim_c,
                image_embed_dim_h,
                image_embed_dim_w,
            ) = image_embedding.shape
            if image_embedding.shape[0] == batch_size:
                image_embedding_tiled = torch.tile(
                    image_embedding[:, None, :, :, :], [1, max_num_queries, 1, 1, 1]
                ).view(
                    batch_size * max_num_queries,
                    image_embed_dim_c,
                    image_embed_dim_h,
                    image_embed_dim_w,
                )
            else:
                # Already early fused with bbox masks.
                image_embedding_tiled = image_embedding
            image_embeddings_tiled.append(image_embedding_tiled)
        sparse_prompt_embeddings = sparse_prompt_embeddings.reshape(
            batch_size * max_num_queries, sparse_embed_dim_1, sparse_embed_dim_2
        )
        masks, iou_pred = self.predict_masks(
            image_embeddings=image_embeddings_tiled,
            image_pe=image_pe,
            sparse_prompt_embeddings=sparse_prompt_embeddings,
        )
        if multimask_output and self.num_multimask_outputs > 1:
            return masks[:, 1:, :], iou_pred[:, 1:]
        else:
            return masks[:, :1, :], iou_pred[:, :1]

    def predict_masks(
        self,
        image_embeddings: List[torch.Tensor],
        image_pe: torch.Tensor,
        sparse_prompt_embeddings: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Predicts masks. See 'forward' for more details."""
        # Concatenate output tokens
        output_tokens = torch.cat(
            [self.iou_token.weight, self.mask_tokens.weight], dim=0
        )
        output_tokens = output_tokens.unsqueeze(0).expand(
            sparse_prompt_embeddings.size(0), -1, -1
        )
        tokens = torch.cat((output_tokens, sparse_prompt_embeddings), dim=1)
        # Expand per-image data in batch direction to be per-mask
        pos_src = torch.repeat_interleave(image_pe, tokens.shape[0], dim=0)
        final_image_embedding = image_embeddings[-1]
        b, c, h, w = final_image_embedding.shape
        hs, src = self.transformer(final_image_embedding, pos_src, tokens)
        iou_token_out = hs[:, 0, :]
        mask_tokens_out = hs[:, 1 : (1 + self.num_mask_tokens), :]

        # Upscale mask embeddings and predict masks using the mask tokens
        upscaled_embedding = src.transpose(1, 2).view(b, c, h, w)

        for upscaling_layer in self.final_output_upscaling_layers:
            upscaled_embedding = upscaling_layer(upscaled_embedding)
        hyper_in_list: List[torch.Tensor] = []
        for i, output_hypernetworks_mlp in enumerate(self.output_hypernetworks_mlps):
            hyper_in_list.append(output_hypernetworks_mlp(mask_tokens_out[:, i, :]))
        hyper_in = torch.stack(hyper_in_list, dim=1)
        b, c, h, w = upscaled_embedding.shape
        masks = (hyper_in @ upscaled_embedding.view(b, c, h * w)).view(b, -1, h, w)
        # Generate mask quality predictions
        iou_pred = self.iou_prediction_head(iou_token_out)
        return masks, iou_pred
