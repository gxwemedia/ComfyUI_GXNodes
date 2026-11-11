"""ComfyUI V3 extension registration for GXNodes."""

from __future__ import annotations

from comfy_api.latest import ComfyExtension, io


class GXNodesExtension(ComfyExtension):
    def __init__(self, nodes: list[type[io.ComfyNode]]):
        self._nodes = nodes

    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return self._nodes
