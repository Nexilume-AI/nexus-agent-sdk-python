"""Small float32 CUDA acceptance workload; no prefilled performance claims."""
import torch


class Model(torch.nn.Module):
    def forward(self, x: torch.Tensor, y: torch.Tensor):
        return x + y
