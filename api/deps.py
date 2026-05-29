"""Shared API singletons.

Both `api.server` and `api.onboard` must use the same instances. OTP and
human-loop responses are in-memory during the MVP, so separate instances make
the customer UI look successful while the running agent never receives input.
"""

from agent.human_loop import HumanLoop
from agent.learning_store import LearningStore
from agent.state_manager import StateManager
from orchestrator import Orchestrator
from tools.ocr_service import OCRService

state_manager = StateManager()
learning_store = LearningStore()
human_loop = HumanLoop(state_manager)
ocr_service = OCRService()
orchestrator = Orchestrator(state_manager, learning_store, human_loop)

