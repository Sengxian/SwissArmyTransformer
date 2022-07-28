import torch

from SwissArmyTransformer.generation.autoregressive_sampling import filling_sequence


class ModelForEvaluation(torch.nn.Module):
    def __init__(self, model):
        super().__init__()

        self.model = model

    @staticmethod
    def process_data(batch):
        return (
            batch["tokens"].to(device=torch.cuda.current_device()).long(),
            batch["position_ids"].to(device=torch.cuda.current_device()).long(),
            batch["attention_mask"].to(device=torch.cuda.current_device()).bool().unsqueeze(1),
        )

    def cond_log_prob(self, batch) -> list[list[float]]:
        """
        @return: Conditional log probability of each option
        """
        tokens, position_ids, attention_mask = self.process_data(batch)
        choices_batch, choice_target_ids_batch = batch["choices"], batch["choice_target_ids"]
        is_single_token = batch["is_single_token"]

        # if torch.distributed.get_rank() == 0:
        #     import pdb
        #
        #     pdb.set_trace()
        #
        # torch.distributed.barrier()

        self.model.eval()
        with torch.no_grad():
            logits, *output_per_layers = self.model(tokens, position_ids, attention_mask, log_attention_weights=None)
            logits_batch = torch.nn.functional.log_softmax(logits, dim=-1)

        # output: [b, sq, vocab]
        log_probs = []

        if is_single_token:  # Single token
            for logits, choices, choice_target_ids in zip(logits_batch, choices_batch, choice_target_ids_batch):
                log_probs.append(logits[choice_target_ids[0], choices].tolist())
        else:  # Multi token
            for output, choices, choice_target_ids in zip(logits_batch, choices_batch, choice_target_ids_batch):
                log_probs_single = []
                for choice, choice_target_id in zip(choices, choice_target_ids):
                    tmp = output[choice_target_id, choice]
                    log_probs_single.append(tmp.sum().tolist())

        return log_probs

    def generate_text(self, sample, strategy, max_length) -> list[list[int]]:
        """
        @return: A list of text model generated, sorted by score in descending order
        """

        seq = torch.squeeze(sample["tokens"].to(device=torch.cuda.current_device()).long())[:max_length]
        context_length = sample["context_length"].to(device=torch.cuda.current_device()).long()
        seq[context_length:] = -1

        def get_masks_and_position_ids(seq):
            tokens = seq.unsqueeze(0)
            attention_mask = sample["attention_mask"].to(device=torch.cuda.current_device()).bool().unsqueeze(1)
            position_ids = sample["position_ids"].to(device=torch.cuda.current_device()).long()
            return tokens, attention_mask, position_ids

        self.model.eval()
        with torch.no_grad():
            output = filling_sequence(
                self.model,
                seq,
                get_masks_and_position_ids=get_masks_and_position_ids,
                batch_size=strategy.num_beams if hasattr(strategy, "num_beams") else 1,
                strategy=strategy,
            )[0]

        if isinstance(output, torch.Tensor):  # different strategies
            output = list(output)

        output_targets = []

        for line in output:
            line = line.tolist()
            unfinished = line.index(-1) if -1 in line else len(line)
            if line[unfinished - 1] in strategy.end_tokens:
                unfinished -= 1
            line = line[context_length:unfinished]
            output_targets.append(line)

        return output_targets
