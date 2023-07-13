#
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Torch.nn.Module Class Defination."""
# Note: Do not import this file unless you have already imported torch, 
# since the model classes inherit torch.nn.Module.
import math
import torch
from torch.nn import functional as F
from packaging.version import Version


def get_torch_version():
    try:
        torch_version = torch.__version__.split('+')[0]
    except ValueError as e:  # pragma: no cover
        assert False, 'Got an unknown version of torch: {}'.format(e)
    version = Version(torch_version)
    return version

PT_VERSION = get_torch_version().release


class QDQLinear(torch.nn.Module):
    def __init__(self, module, scale=1, zero_point=0, dtype=torch.quint8):
        super().__init__()
        if PT_VERSION < Version("1.13.0").release:
            import torch.nn.quantized as nnq
        else:
            import torch.ao.nn.quantized as nnq
        self.add_module('quant', nnq.Quantize(scale, zero_point, dtype))
        self.add_module('dequant', nnq.DeQuantize())
        self.add_module('module', module)
        self.qdq_weight()
     
    @property
    def weight(self):
        return self.module.weight

    def forward(self, X):
        X = self.quant(X)
        X = self.dequant(X)
        X = self.module(X)
        return X

    def qdq_weight(self):
        # update weight w/ QDQ
        from .smooth_quant import quant_dequant_w
        weith_qdq = quant_dequant_w(self.module)
        self.module.weight = torch.nn.Parameter(weith_qdq)


class SQLinearWrapper(torch.nn.Module):
    def __init__(self, module, input_scale, input_minmax, alpha=0.5, dtype=torch.quint8):
        super().__init__()
        self.register_buffer('input_scale', input_scale)
        self.alpha = alpha
        self.dtype = dtype
        # calculate and only save scale, zero_point to avoid memory usage
        self.scale, self.zero_point = self._calculate_qparams(input_scale, input_minmax, dtype)
        self.add_module('sq_linear', module)
        self.ipex = False  # a flag used for ipex inference
    
    @property
    def weight(self):
        return self.sq_linear.weight

    def forward(self, X):
        if self.ipex:
            X = self.sq_linear(X)
        else:
            X = torch.mul(X, self.input_scale)
            X = self.sq_linear(X)
        return X

    def _calculate_qparams(self, input_scale, input_minmax, dtype=torch.quint8):
        # calculate scale and zero_point
        if dtype == torch.quint8:
            quant_min, quant_max = 0, 255
        min_val = torch.min(input_minmax[0] * input_scale)
        max_val = torch.max(input_minmax[1] * input_scale)
        # work when min_val bigger than zero.
        min_val_neg = torch.min(min_val, torch.zeros_like(min_val))
        max_val_pos = torch.max(max_val, torch.zeros_like(max_val))
        scale = (max_val_pos - min_val_neg) / float(quant_max - quant_min)
        scale = torch.max(scale, torch.tensor([torch.finfo(torch.float32).eps]))
        zero_point = quant_min - torch.round(min_val_neg / scale).to(torch.int)
        zero_point = torch.clamp(zero_point, quant_min, quant_max)
        return scale, zero_point

    def _get_weight_scale(self):
        # get weight scale and zero_point
        from torch.ao.quantization.observer import default_per_channel_weight_observer
        obs = default_per_channel_weight_observer()
        obs(self.sq_linear.weight)
        scale, _ = obs.calculate_qparams()
        return scale

    def _recover_sq_linear(self):
        # remove mul and reset sq_linear for ipex inference
        scale = self.input_scale.view(1, self.input_scale.shape[0])
        with torch.no_grad():
            self.sq_linear.weight *= scale


def _wrapper_sq_linear(tmp_model, input_scale_dict):
    """Help function to generate a fake SmoothQuant model for loading weights"""
    class SQLinearWrapper(torch.nn.Module):
        def __init__(self, module, input_scale):
            super().__init__()
            self.register_buffer('input_scale', input_scale)
            self.add_module('sq_linear', module)

        def forward(self, X):
            X = torch.mul(X, self.input_scale)
            X = self.sq_linear(X)
            return X

    module_name_list = input_scale_dict.keys()
    from .smooth_quant import get_module, set_module
    for name in module_name_list:
        module = get_module(tmp_model, name)
        input_scale = input_scale_dict[name]
        new_module = SQLinearWrapper(module, input_scale)
        set_module(tmp_model, name, new_module)
    return tmp_model


def _wrapper_qdq_linear(tmp_model, module_name_list=[]):
    """Help function to generate a fake QDQ model for loading weights"""
    from .smooth_quant import get_module, set_module
    for name in module_name_list:
        module = get_module(tmp_model, name)
        new_module = QDQLinear(module)
        set_module(tmp_model, name, new_module)
    return tmp_model


class WeightOnlyLinear(torch.nn.Module):
    def __init__(self, in_features, out_features, bits, groupsize):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.bits = bits
        self.groupsize = groupsize if groupsize != -1 else in_features
        self.n_pack = 32 // self.bits

        self.register_buffer(
            'packed_weight', 
            torch.zeros(
                (out_features, math.ceil(in_features / self.n_pack)), 
                dtype=torch.int32,
            )
        )
        self.register_buffer(
            'scale', 
            torch.zeros(
                (out_features, math.ceil(in_features / self.groupsize)), 
                dtype=torch.float,
            )
        )

    def pack(self, int_weight, scale, zp, bias):
        if bias is not None:
            self.register_buffer('bias', torch.zeros(self.out_features, dtype=torch.float))
        else:
            self.bias = None
        self.bias = bias
        assert scale.shape == self.scale.shape, "Scale shape is mismatched."
        self.scale = scale
        origin_shape = int_weight.shape
        target_shape = self.packed_weight.shape
        assert origin_shape[0] == target_shape[0], "output channels mismatch, please check."
        mask = torch.tensor(2**self.bits - 1, dtype=torch.int32)

        # pack weight
        for i in range(target_shape[0]):
            for j in range(target_shape[1]):
                start = self.n_pack * j
                end = self.n_pack * (j + 1)
                tmp = int_weight[i][start: end].type(torch.int32)
                for e in range(len(tmp)):
                    tmp[e] &= mask
                    tmp[e] = tmp[e] << self.bits * (self.n_pack - 1 - e)
                    self.packed_weight[i][j] |= tmp[e]

        if zp is not None:
            # pack zero_points
            self.register_buffer(
                'packed_zp', 
                torch.zeros(
                    (self.out_features, math.ceil(self.in_features / self.groupsize / self.n_pack)), 
                    dtype=torch.int32,
                )
            )
            target_shape = self.packed_zp.shape
            for i in range(target_shape[0]):
                for j in range(target_shape[1]):
                    start = self.n_pack * j
                    end = self.n_pack * (j + 1)
                    tmp = zp[i][start: end].type(torch.int32)
                    for e in range(len(tmp)):
                        tmp[e] &= mask
                        tmp[e] = tmp[e] << self.bits * (self.n_pack - 1 - e)
                        self.packed_zp[i][j] |= tmp[e]

    def recover(self):
        mask = torch.tensor(2**self.bits - 1, dtype=torch.int32)
        if hasattr(self, 'packed_zp'):
            weight_dtype = torch.uint8
        else:
            weight_dtype = torch.int8
        # unpack weight
        weight = torch.zeros(self.out_features, self.in_features, dtype=weight_dtype)
        origin_shape = weight.shape
        target_shape = self.packed_weight.shape
        for i in range(target_shape[0]):
            for j in range(target_shape[1]):
                for e in range(self.n_pack):
                    index = j * self.n_pack + e
                    if index >= origin_shape[1]:
                        continue
                    tmp = self.packed_weight[i][j]
                    tmp = tmp << 32 - self.bits * (self.n_pack - e)
                    tmp = tmp >> 32 - self.bits
                    if weight_dtype == torch.uint8:
                        tmp &= mask # remove sign bit
                    weight[i][index] = tmp.type(weight_dtype)
        # unpack zero_point
        if hasattr(self, 'packed_zp'):
            zp_dtype = torch.int32 # to avoid overflow when weight-zp
            zp = torch.zeros(self.scale.shape, dtype=zp_dtype)
            origin_shape = zp.shape
            target_shape = self.packed_zp.shape
            for i in range(target_shape[0]):
                for j in range(target_shape[1]):
                    for e in range(self.n_pack):
                        index = j * self.n_pack + e
                        if index >= origin_shape[1]:
                            continue
                        tmp = self.packed_zp[i][j]
                        tmp = tmp << 32 - self.bits * (self.n_pack - e)
                        tmp = tmp >> 32 - self.bits
                        tmp &= mask
                        zp[i][index] = tmp.type(zp_dtype)
            # recover fp32 weight with int_weight, scale, and zero_point
            left_element = self.in_features % self.groupsize 
            if left_element != 0:
                split_index = self.in_features // self.groupsize  * self.groupsize
                weight1 = weight[:, :-split_index].reshape(-1, self.groupsize)
                scale1 = self.scale[:, :-1].reshape(-1, 1)
                zp1 = zp[:, :-1].reshape(-1, 1)
                weight1 = ((weight1 - zp1) * scale1).reshape(self.out_features, -1)
                weight2 = weight[:, -split_index:]
                scale2 = self.scale[:, -1:]
                zp2 = zp[:, -1].reshape(-1, 1)
                weight2 = ((weight2 - zp2) * scale2)
                fp32_weight = torch.cat((weight1, weight2), dim=1)
            else:
                weight = weight.reshape(-1, self.groupsize)
                scale = self.scale.reshape(-1, 1)
                zp = zp.reshape(-1, 1)
                fp32_weight = ((weight - zp) * scale).reshape(self.out_features, -1)
        else:
            # recover fp32 weight with int_weight, scale
            left_element = self.in_features % self.groupsize 
            if left_element != 0:
                split_index = self.in_features // self.groupsize  * self.groupsize
                weight1 = weight[:, :split_index].reshape(-1, self.groupsize)
                scale1 = self.scale[:, :-1].reshape(-1, 1)
                weight1 = (weight1 * scale1).reshape(self.out_features, -1)
                weight2 = weight[:, split_index:]
                scale2 = self.scale[:, -1:]
                weight2 = (weight2 * scale2)
                fp32_weight = torch.cat((weight1, weight2), dim=1)
            else:
                weight = weight.reshape(-1, self.groupsize)
                scale = self.scale.reshape(-1, 1)
                fp32_weight = (weight * scale).reshape(self.out_features, -1)
        return fp32_weight

    def forward(self, input):
        weight = self.recover()
        return F.linear(input, weight, self.bias)

    def extra_repr(self) -> str:
        return 'in_features={}, out_features={}, bits={}, group_size={}, bias={}'.format(
            self.in_features, self.out_features, self.bits, self.groupsize, self.bias is not None
        )
