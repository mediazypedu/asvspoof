# Copyright (c) 2021, Soohwan Kim. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import List
import torch
import torch.nn as nn
from torch import Tensor
from typing import Tuple

from transformer_models.modules import (
    LayerNorm,
    ResidualConnectionModule,
    Linear,
)
from transformer_models.conformer.modules import (
    FeedForwardModule,
    MultiHeadedSelfAttentionModule,
    ConformerConvModule,
)
from transformer_models.longformer.longformer import (
    LongformerSelfAttention,
)
import sandbox.block_nn as nii_nn
class localBlock(nn.Module):
    """
    Conformer block contains two Feed Forward modules sandwiching the Multi-Headed Self-Attention module
    and the Convolution module. This sandwich structure is inspired by Macaron-Net, which proposes replacing
    the original feed-forward layer in the Transformer block into two half-step feed-forward layers,
    one before the attention layer and one after.

    Args:
        encoder_dim (int, optional): Dimension of conformer encoder
        num_attention_heads (int, optional): Number of attention heads
        feed_forward_expansion_factor (int, optional): Expansion factor of feed forward module
        conv_expansion_factor (int, optional): Expansion factor of conformer convolution module
        feed_forward_dropout_p (float, optional): Probability of feed forward module dropout
        attention_dropout_p (float, optional): Probability of attention module dropout
        conv_dropout_p (float, optional): Probability of conformer convolution module dropout
        conv_kernel_size (int or tuple, optional): Size of the convolving kernel
        half_step_residual (bool): Flag indication whether to use half step residual or not
        device (torch.device): torch device (cuda or cpu)

    Inputs: inputs
        - **inputs** (batch, time, dim): Tensor containing input vector

    Returns: outputs
        - **outputs** (batch, time, dim): Tensor produces by conformer block.
    """
    def __init__(
            self,
            encoder_dim: int = 512,
            num_attention_heads: int = 8,
            feed_forward_expansion_factor: int = 4,
            feed_forward_dropout_p: float = 0.1,
            attention_dropout_p: float = 0.1,
            half_step_residual: bool = True,

            attention_window: int = 2,
            attention_dilation: int = 1,
            autoregressive: bool = False,
            attention_mode: str = 'sliding_chunks',

            device: torch.device = 'cuda',
    ):
        super(localBlock, self).__init__()
        self.device = device
        if half_step_residual:
            self.feed_forward_residual_factor = 0.5
        else:
            self.feed_forward_residual_factor = 1
        self.sequential = nn.Sequential(
            # ResidualConnectionModule(
            #     module=FeedForwardModule(
            #         encoder_dim=encoder_dim,
            #         expansion_factor=feed_forward_expansion_factor,
            #         dropout_p=feed_forward_dropout_p,
            #     ),
            #     module_factor=self.feed_forward_residual_factor,
            # ),
            ResidualConnectionModule(
                module=LongformerSelfAttention(
                    hidden_size=encoder_dim,
                    num_attention_heads=num_attention_heads,
                    attention_probs_dropout_prob=attention_dropout_p,
                    attention_window=attention_window,
                    attention_dilation=attention_dilation,
                    attention_mode=attention_mode,
                    autoregressive=autoregressive,
                )
            )
            #LayerNorm(encoder_dim),
        )

    def forward(self, inputs: Tensor):
        outputs, _ = self.sequential[0](inputs.to(self.device))
        #outputs, _ = self.sequential[1](outputs)
        #outputs = self.sequential[1](outputs)
        return outputs, None
class SEBlock(nn.Module):
    def __init__(self, channel, r=2):
        super(SEBlock, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel//r, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel//r, channel, bias=False),
            nn.Sigmoid(),
        )

    def forward(self, x):
        b, c , _, _ = x.size()
        # Squeeze
        y = self.avg_pool(x).view(b, c)
        # Excitation
        y = self.fc(y).view(b, c, 1, 1)
        # Fscale
        y = torch.mul(x, y)
        return y
class ConformerEncoder(torch.nn.Module):
    """
    Conformer encoder first processes the input with a convolution subsampling layer and then
    with a number of conformer blocks.

    Args:
        input_dim (int, optional): Dimension of input vector
        encoder_dim (int, optional): Dimension of conformer encoder
        num_layers (int, optional): Number of conformer blocks
        num_attention_heads (int, optional): Number of attention heads
        feed_forward_expansion_factor (int, optional): Expansion factor of feed forward module
        conv_expansion_factor (int, optional): Expansion factor of conformer convolution module
        feed_forward_dropout_p (float, optional): Probability of feed forward module dropout
        attention_dropout_p (float, optional): Probability of attention module dropout
        conv_dropout_p (float, optional): Probability of conformer convolution module dropout
        conv_kernel_size (int or tuple, optional): Size of the convolving kernel
        half_step_residual (bool): Flag indication whether to use half step residual or not
        device (torch.device): torch device (cuda or cpu)

    Inputs: inputs, input_lengths
        - **inputs** (batch, time, dim): Tensor containing input vector
        - **input_lengths** (batch): list of sequence input lengths

    Returns: outputs, output_lengths
        - **outputs** (batch, out_channels, time): Tensor produces by conformer encoder.
        - **output_lengths** (batch): list of sequence output lengths
    """
    def __init__(
            self,
            input_dim: int = 60,
            encoder_dim: int = 96,
            num_layers: int = 1,
            num_attention_heads: int = 4,
            feed_forward_expansion_factor: int = 2,
            feed_forward_dropout_p: float = 0.1,
            attention_dropout_p: float = 0.1,
            half_step_residual: bool = False,
            attention_window: List[int] = [2, 2, 2, 2, 2, 2, 2, 2],
            attention_dilation: List[int] = [1, 1, 1, 1, 1, 1, 1, 1],
            autoregressive: bool = False,
            attention_mode: str = 'sliding_chunks',
            device: torch.device = 'cuda',
    ):
        super(ConformerEncoder, self).__init__()
        self.conv_subsample = torch.nn.Sequential(
                    torch.nn.Conv2d(1, 64, [5, 5], 1, padding=[2, 2]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.MaxPool2d([2, 2], [2, 2]),
                    SEBlock(32),
                    torch.nn.Conv2d(32, 64, [1, 1], 1, padding=[0, 0]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.BatchNorm2d(32, affine=False),
                    SEBlock(32),
                    torch.nn.Conv2d(32, 96, [3, 3], 1, padding=[1, 1]),
                    nii_nn.MaxFeatureMap2D(),

                    torch.nn.MaxPool2d([2, 2], [2, 2]),
                    torch.nn.BatchNorm2d(48, affine=False),
                    SEBlock(48),

                    torch.nn.Conv2d(48, 96, [1, 1], 1, padding=[0, 0]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.BatchNorm2d(48, affine=False),
                    SEBlock(48),
                    torch.nn.Conv2d(48, 128, [3, 3], 1, padding=[1, 1]),
                    nii_nn.MaxFeatureMap2D(),

                    torch.nn.MaxPool2d([2, 2], [2, 2]),
                    SEBlock(64),
                    torch.nn.Conv2d(64, 128, [1, 1], 1, padding=[0, 0]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.BatchNorm2d(64, affine=False),
                    SEBlock(64),
                    torch.nn.Conv2d(64, 64, [3, 3], 1, padding=[1, 1]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.BatchNorm2d(32, affine=False),
                    SEBlock(32),
                    torch.nn.Conv2d(32, 64, [1, 1], 1, padding=[0, 0]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.BatchNorm2d(32, affine=False),
                    SEBlock(32),
                    torch.nn.Conv2d(32, 64, [3, 3], 1, padding=[1, 1]),
                    nii_nn.MaxFeatureMap2D(),
                    torch.nn.MaxPool2d([2, 2], [2, 2]),

                    torch.nn.Dropout(0.7)
                )

        # self.input_projection = nn.Sequential(
        #     # Nan定位
        #     # LayerNorm(256 * (input_dim//16)),
        #     Linear(32 * (input_dim // 16), encoder_dim),
        #     #torch.nn.Dropout(input_dropout_p)
        #     # 第二次debug发现这里的dropout 0.9 会导致Nan
        #     # nn.Dropout(0.9),
        # )
        self.layers_1 = nn.ModuleList([
            localBlock(
                encoder_dim=encoder_dim,
                num_attention_heads=num_attention_heads,
                feed_forward_expansion_factor=feed_forward_expansion_factor,
                feed_forward_dropout_p=feed_forward_dropout_p,
                attention_dropout_p=attention_dropout_p,
                half_step_residual=half_step_residual,
                attention_window=attention_window[layer_id],
                attention_dilation=attention_dilation[layer_id],
                autoregressive=autoregressive,
                attention_mode=attention_mode,
                device=device,
            ).to(device) for layer_id in range(num_layers)]
        )
    def forward(self, inputs: Tensor):
        outputs = self.conv_subsample(inputs)
        #BxCxWxH
        outputs = outputs.permute(0, 2, 1, 3).contiguous()
        batch_size, frame_num = outputs.shape[0],outputs.shape[1]
        outputs = outputs.view(batch_size, frame_num, -1)
        #outputs = self.input_projection(outputs)
        for layer in self.layers_1:
            outputs, _ = layer(outputs)
        return outputs, None
