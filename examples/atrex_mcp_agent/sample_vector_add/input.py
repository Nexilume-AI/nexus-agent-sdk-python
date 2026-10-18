import torch


def _make_inputs(n: int):
    return {'x': torch.randn(n, device='cuda', dtype=torch.float32),
            'y': torch.randn(n, device='cuda', dtype=torch.float32)}
