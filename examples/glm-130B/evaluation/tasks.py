import torch
import time
import numpy as np
import torch.distributed as dist

from typing import Dict, Callable, Type, Tuple, List
from abc import ABC, abstractmethod
from glob import glob
from os.path import join, relpath
from collections import defaultdict
from functools import reduce

from SwissArmyTransformer.generation.sampling_strategies import BaseStrategy
from SwissArmyTransformer.tokenization.icetk_glm_130B.ice_tokenizer import _IceTokenizer

from .configs import BaseConfig, GenerationTaskConfig, MultiChoiceTaskConfig
from .model import ModelForEvaluation
from .dataset import ZeroShotDataset
from .utils import build_data_loader, gather_result, print_rank_0
from .strategies import DeterminedBeamSearchStrategy
from .metrics import qa_exact_match, qa_f1, accuracy_metric

DEFAULT_METRICS = {"EM": qa_exact_match, "F1": qa_f1, "Accuracy": accuracy_metric}


class BaseTask(ABC):
    model: ModelForEvaluation
    tokenizer: _IceTokenizer
    config: BaseConfig
    file_groups: Dict[str, List[str]]

    @classmethod
    def config_class(cls) -> Type[BaseConfig]:
        return BaseConfig

    @property
    def metrics(self) -> Dict[str, Callable]:
        return {metric: DEFAULT_METRICS[metric] for metric in self.config.metrics}

    def __init__(self, model: ModelForEvaluation, tokenizer: _IceTokenizer, config: BaseConfig):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config
        self.config.metrics = list(self.metrics.keys())

        self.file_groups = self.get_file_groups()
        self.verbose = dist.get_rank() == 0

    def get_file_groups(self):
        pattern_group = {}
        if isinstance(self.config.file_pattern, str):
            pattern_group["all"] = self.config.file_pattern
        else:
            pattern_group = self.config.file_pattern
        return {
            name: [
                relpath(path, start=self.config.path)
                for path in sorted(glob(join(self.config.path, pattern), recursive=True))
            ]
            for name, pattern in pattern_group.items()
        }

    def build_dataset(self, file):
        return ZeroShotDataset(
            join(self.config.path, file),
            max_seq_length=self.config.max_seq_length,
            use_task_mask=self.config.use_task_mask,
            unidirectional=self.config.unidirectional,
        )

    def evaluate(self):
        dist.barrier()
        start = time.time()
        print_rank_0("\n")
        print_rank_0(f"{self.config}")
        print_rank_0(f"Evaluating task {self.config.name}:")

        result_dict_all = {}

        for group_name, filelist in self.file_groups.items():
            print_rank_0(f"    Evaluating group {group_name}:")

            result_dict_group = {}
            for file in filelist:
                dataset = self.build_dataset(file)
                dataloader = build_data_loader(dataset, micro_batch_size=1, num_workers=1, drop_last=False)

                prediction = []
                with torch.no_grad():
                    for _, batch in enumerate(dataloader):
                        prediction.append(self.predict_single_batch(batch))

                prediction = gather_result(prediction, len(dataset))
                result_dict = {key: metric(prediction, dataset.data) for key, metric in self.metrics.items()}
                result_dict_group[file] = (result_dict, len(dataset))

                if self.verbose:
                    self.report_single_metrics(file, result_dict)

            result_dict_all[group_name] = result_dict_group

        print_rank_0(f"Evaluation results of task {self.config.name}:")

        if self.verbose:
            for group_name, result_dict_group in result_dict_all.items():
                self.report_group_metrics(group_name, result_dict_group)
            self.report_overall_metrics(
                {k: v for result_dict_group in result_dict_all.values() for k, v in result_dict_group.items()},
            )

        print_rank_0(f"Finish task {self.config.name} in {time.time() - start:.1f}s.")

    def report_single_metrics(self, file: str, result_dict: Dict[str, float]):
        output_str = f"        Finish {file}"
        for key, value in result_dict.items():
            output_str += f", {key} = {value:.3f}"
        print_rank_0(output_str)

    @staticmethod
    def calc_group_metrics(result_dict_group: Dict[str, Tuple[Dict[str, float], int]]):
        metrics_dict = defaultdict(lambda: [])
        weight = []
        for file, (result_dict, length) in result_dict_group.items():
            for key, value in result_dict.items():
                metrics_dict[key].append(value)
            weight.append(length)
        return {
            name: {
                "max": np.max(value),
                "median": np.median(value),
                "average": np.average(value, weights=weight),
            }
            for name, value in metrics_dict.items()
        }

    def report_group_metrics(self, group_name, result_dict_group: Dict[str, Tuple[Dict[str, float], int]], level=1):
        stats_dict = self.calc_group_metrics(result_dict_group)
        if len(stats_dict) == 1:
            name, stats = next(iter(stats_dict.items()))
            print_rank_0(
                "    " * level + f"Group {group_name} {name}: max = {stats['max']:.3f}, "
                f"median = {stats['median']:.3f}, average = {stats['average']:.3f}"
            )
        else:
            print_rank_0("    " * level + f"  Group {group_name}: ")
            for name, stats in stats_dict.items():
                print(
                    "    " * (level + 1) + f"Metric {name}: max = {stats['max']:.3f}, "
                    f"median = {stats['median']:.3f}, average = {stats['average']:.3f}"
                )

    def report_overall_metrics(self, result_dict_all: Dict[str, Tuple[Dict[str, float], int]]):
        pass

    @abstractmethod
    def predict_single_batch(self, batch):
        pass


class GenerationTask(BaseTask, ABC):
    config: GenerationTaskConfig

    @classmethod
    def config_class(cls):
        return GenerationTaskConfig

    def __init__(self, model: ModelForEvaluation, tokenizer: _IceTokenizer, config: GenerationTaskConfig):
        super(GenerationTask, self).__init__(model, tokenizer, config)

        end_tokens = [tokenizer.get_command("eop"), tokenizer.get_command("eos")]
        if self.config.sampling_strategy == "BaseStrategy":
            self.strategy = BaseStrategy(temperature=1.0, top_k=1, end_tokens=end_tokens)
        elif self.config.sampling_strategy == "BeamSearchStrategy":
            self.strategy = DeterminedBeamSearchStrategy(
                self.config.num_beams,
                length_penalty=self.config.length_penalty,
                consider_end=True,
                end_tokens=end_tokens,
                no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                min_tgt_length=self.config.min_tgt_length,
            )
        else:
            raise ValueError(f"unknown strategy {self.config.sampling_strategy}")

    def predict_single_batch(self, batch):
        outputs = self.model.generate_text(batch, self.strategy, max_length=self.config.max_seq_length)
        return outputs[0]


class MultiChoiceTask(BaseTask, ABC):
    config: MultiChoiceTaskConfig

    @classmethod
    def config_class(cls):
        return MultiChoiceTaskConfig

    def predict_single_batch(self, batch):
        return np.argmax(self.model.cond_log_prob(batch))