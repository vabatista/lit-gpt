import sys
import os
import json
import time
from pathlib import Path
from typing import Literal, Optional
from tqdm import tqdm   
import lightning as L
import torch
from lightning.fabric.plugins import BitsandbytesPrecision
from lightning.fabric.strategies import FSDPStrategy
from datasets import load_metric

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from generate.base import generate
from lit_gpt import Tokenizer
from lit_gpt.lora import GPT, Block, Config, merge_lora_weights
from lit_gpt.utils import check_valid_checkpoint_dir, get_default_supported_precision, gptq_quantization, lazy_load
from scripts.prepare_alpaca import generate_prompt

lora_r = 8
lora_alpha = 16
lora_dropout = 0.05
lora_query = True
lora_key = True
lora_value = True
lora_projection = True
lora_mlp = True
lora_head = True

def load_data(input_file):
    with open(input_file, 'r') as file:
        json_list = list(file)
    
    data = []
    # [1:] removes the header
    for item in json_list[1:]:
        item = json.loads(item)
        data.append(item)

    return data


def get_contexts_questions_answers(json_data):
    data_list = []

    for paragraph in json_data:
        context = paragraph['context']
        for qa in paragraph['qas']:
            question = qa['question']
            if qa['detected_answers']:
                answer = list(set([a['text'] for a in qa['detected_answers']]))
            elif qa['answers']:
                answer = list(qa['answers'])

            data_dict = {
                'context': context,
                'question': question,
                'answer': answer
            }
            data_list.append(data_dict)

    return data_list

def main(
    prompt: str = "What food do lamas eat?",
    input: str = "",
    lora_path: Path = Path("out/lora/squad/lit_model_lora_finetuned.pth"),
    checkpoint_dir: Path = Path("checkpoints/stabilityai/stablelm-base-alpha-3b"),
    quantize: Optional[Literal["bnb.nf4", "bnb.nf4-dq", "bnb.fp4", "bnb.fp4-dq", "bnb.int8", "gptq.int4"]] = None,
    max_new_tokens: int = 32,
    top_k: int = 200,
    temperature: float = 0.1,
    strategy: str = "auto",
    devices: int = 1,
    precision: Optional[str] = None,
    input_squad_file: str = None,
    output_dir: Path = Path("out/preds/squad")
) -> None:
    """Generates a response based on a given instruction and an optional input.
    This script will only work with checkpoints from the instruction-tuned GPT-LoRA model.
    See `finetune/lora.py`.

    Args:
        prompt: The prompt/instruction (Alpaca style).
        input: Optional input (Alpaca style).
        lora_path: Path to the checkpoint with trained adapter weights, which are the output of
            `finetune/lora.py`.
        checkpoint_dir: The path to the checkpoint folder with pretrained GPT weights.
        quantize: Whether to quantize the model and using which method:
            - bnb.nf4, bnb.nf4-dq, bnb.fp4, bnb.fp4-dq: 4-bit quantization from bitsandbytes
            - bnb.int8: 8-bit quantization from bitsandbytes
            - gptq.int4: 4-bit quantization from GPTQ
            for more details, see https://github.com/Lightning-AI/lit-gpt/blob/main/tutorials/quantize.md
        max_new_tokens: The number of generation steps to take.
        top_k: The number of top most probable tokens to consider in the sampling process.
        temperature: A value controlling the randomness of the sampling process. Higher values result in more random
            samples.
        strategy: Indicates the Fabric strategy setting to use.
        devices: How many devices to use.
        precision: Indicates the Fabric precision setting to use.
    """
    precision = precision or get_default_supported_precision(training=False)

    plugins = None
    if quantize is not None:
        if devices > 1:
            raise NotImplementedError(
                "Quantization is currently not supported for multi-GPU training. Please set devices=1 when using the"
                " --quantize flag."
            )
        if quantize.startswith("bnb."):
            if "mixed" in precision:
                raise ValueError("Quantization and mixed precision is not supported.")
            dtype = {"16-true": torch.float16, "bf16-true": torch.bfloat16, "32-true": torch.float32}[precision]
            plugins = BitsandbytesPrecision(quantize[4:], dtype)
            precision = None

    if strategy == "fsdp":
        strategy = FSDPStrategy(auto_wrap_policy={Block}, cpu_offload=False)

    fabric = L.Fabric(devices=devices, precision=precision, strategy=strategy, plugins=plugins)
    fabric.launch()

    check_valid_checkpoint_dir(checkpoint_dir)

    config = Config.from_json(
        checkpoint_dir / "lit_config.json",
        r=lora_r,
        alpha=lora_alpha,
        dropout=lora_dropout,
        to_query=lora_query,
        to_key=lora_key,
        to_value=lora_value,
        to_projection=lora_projection,
        to_mlp=lora_mlp,
        to_head=lora_head,
    )

    if quantize is not None and devices > 1:
        raise NotImplementedError
    if quantize == "gptq.int4":
        model_file = "lit_model_gptq.4bit.pth"
        if not (checkpoint_dir / model_file).is_file():
            raise ValueError("Please run `python quantize/gptq.py` first")
    else:
        model_file = "lit_model.pth"
    checkpoint_path = checkpoint_dir / model_file

    fabric.print(f"Loading model {str(checkpoint_path)!r} with {config.__dict__}", file=sys.stderr)
    t0 = time.perf_counter()
    with fabric.init_module(empty_init=True), gptq_quantization(quantize == "gptq.int4"):
        model = GPT(config)
    fabric.print(f"Time to instantiate model: {time.perf_counter() - t0:.02f} seconds.", file=sys.stderr)

    t0 = time.perf_counter()
    checkpoint = lazy_load(checkpoint_path)
    lora_checkpoint = lazy_load(lora_path)
    checkpoint.update(lora_checkpoint.get("model", lora_checkpoint))
    model.load_state_dict(checkpoint)
    fabric.print(f"Time to load the model weights: {time.perf_counter() - t0:.02f} seconds.", file=sys.stderr)

    model.eval()
    merge_lora_weights(model)
    model = fabric.setup(model)

    tokenizer = Tokenizer(checkpoint_dir)

    ##
    data = load_data(input_squad_file)
    triples = get_contexts_questions_answers(data)
    
    predictions_with_correct_context = []
    references = []
    t0 = time.perf_counter()
    for idx, triple in enumerate(tqdm(triples)):
        question, answers, context = triple['question'], triple['answer'], triple['context']
        references.append({'id': str(idx), 'answers': {'answer_start': [context.find(answer) for answer in answers], 'text': [answer for answer in answers]}})

        sample = {"instruction": question, "input": context}
        prompt = generate_prompt(sample)
        encoded = tokenizer.encode(prompt, device=fabric.device)
        prompt_length = encoded.size(0)
        max_returned_tokens = prompt_length + max_new_tokens
        try: 
            with fabric.init_tensor():
                # set the max_seq_length to limit the memory usage to what we need
                model.max_seq_length = max_returned_tokens
                # enable the kv cache
                model.set_kv_cache(batch_size=1)

            y = generate(model, encoded, max_returned_tokens, temperature=temperature, top_k=top_k, eos_id=tokenizer.eos_id)
            

            output = tokenizer.decode(y)
            output = output.split("### Response:")[1].strip()
        except:
            output = ''
        predictions_with_correct_context.append({'id': str(idx), 'prediction_text':  output})
    
    t = time.perf_counter() - t0
    fabric.print(f"\n\nTime for inference: {t:.02f} sec total", file=sys.stderr)

    ## Save both predictions and references to files for evaluation
    ## if output_dir does not exist, create it
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    predictions_with_correct_context_file = os.path.join(output_dir,'predictions_with_correct_context.json')
    references_file = os.path.join(output_dir, 'references.json')
    
    with open(predictions_with_correct_context_file, 'w') as f:
        json.dump(predictions_with_correct_context, f)

    with open(references_file, 'w') as f:
        json.dump(references, f)

    squad_metric = load_metric('squad')
    results = squad_metric.compute(predictions=predictions_with_correct_context, references=references)
    print(results)
    print(f"Exact match: {results['exact_match']:.2f}")
    print(f"F1 score: {results['f1']:.2f}")
    
    if fabric.device.type == "cuda":
        fabric.print(f"Memory used: {torch.cuda.max_memory_allocated() / 1e9:.02f} GB", file=sys.stderr)


if __name__ == "__main__":
    from jsonargparse import CLI

    torch.set_float32_matmul_precision("high")
    CLI(main)