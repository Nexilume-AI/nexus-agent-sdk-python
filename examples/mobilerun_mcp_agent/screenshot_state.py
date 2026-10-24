"""Reuse only the image captured for the current decision, never a timed cache."""
from mobilerun.tools.ui.screenshot_provider import ScreenshotOnlyStateProvider
from mobilerun.agent.utils.vision_sizing import VisionResizePolicy, model_uses_normalized_coordinates

class StateDriver:
    def __init__(self, driver): self.driver = driver
    def __getattr__(self, name): return getattr(self.driver, name)
    async def screenshot(self): return await self.driver.consume_state_screenshot()

class DecisionStateProvider(ScreenshotOnlyStateProvider):
    def __init__(self, driver, llm, max_side=1280):
        super().__init__(StateDriver(driver),
            vision_resize_policy=VisionResizePolicy.from_llms([llm], max_side_cap=max_side),
            use_normalized=model_uses_normalized_coordinates(str(llm.model)))
    async def get_final_state(self):
        return await super().get_state()
