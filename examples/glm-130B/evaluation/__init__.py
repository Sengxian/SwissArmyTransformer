from .configs import *
from .model import ModelForEvaluation
from .tasks import BaseTask, GenerationTask, MultiChoiceTask
from .metrics import qa_evaluate
from .strategies import DeterminedBeamSearchStrategy
from .utils import print_rank_0

DEFAULT_CLASS = {TaskType.GENERATION: GenerationTask, TaskType.MULTICHOICE: MultiChoiceTask}
