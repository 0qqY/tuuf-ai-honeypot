"""Qwen3-1.7B QLoRA fine-tune (WSL, 8 GB VRAM).
Kullanım:
  python train_qlora.py --data data/sft.jsonl --out out/sepetly-ops
  python train_qlora.py --export out/sepetly-ops      # GGUF Q4_K_M üretir
Veri formatı (JSONL): {"messages":[...], "tools":[...] (opsiyonel)}
"""
import argparse
import json

from unsloth import FastLanguageModel  # unsloth her zaman ilk import edilmeli
from unsloth.chat_templates import train_on_responses_only
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer

BASE = "unsloth/Qwen3-1.7B"
MAX_LEN = 2048


def train(args):
    model, tok = FastLanguageModel.from_pretrained(BASE, max_seq_length=MAX_LEN, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=32, lora_dropout=0.0, bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407,
    )

    def fmt(ex):
        tools = ex.get("tools")
        if isinstance(tools, str):
            tools = json.loads(tools)
        text = tok.apply_chat_template(ex["messages"], tools=tools or None, tokenize=False,
                                       enable_thinking=False)
        return {"text": text}

    ds = load_dataset("json", data_files=args.data, split="train").map(fmt)
    ds = ds.train_test_split(test_size=0.05, seed=3407)

    trainer = SFTTrainer(
        model=model, tokenizer=tok, train_dataset=ds["train"], eval_dataset=ds["test"],
        args=SFTConfig(
            dataset_text_field="text", per_device_train_batch_size=2, gradient_accumulation_steps=8,
            num_train_epochs=args.epochs, learning_rate=2e-4, lr_scheduler_type="cosine", warmup_ratio=0.03,
            logging_steps=10, eval_strategy="steps", eval_steps=100, save_steps=200, save_total_limit=2,
            optim="adamw_8bit", bf16=True, output_dir=args.out + "-ckpt", report_to="none", seed=3407,
        ),
    )
    # Sadece asistan cevaplarından öğren (saldırgan prompt'larını ezberleme)
    trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n",
                                      response_part="<|im_start|>assistant\n")
    trainer.train()
    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)


def export(path):
    model, tok = FastLanguageModel.from_pretrained(path, max_seq_length=MAX_LEN, load_in_4bit=True)
    model.save_pretrained_gguf(path + "-gguf", tok, quantization_method="q4_k_m")
    print("GGUF ->", path + "-gguf  (VPS'te models/ klasörüne kopyala)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="data/sft.jsonl")
    p.add_argument("--out", default="out/sepetly-ops")
    p.add_argument("--epochs", type=float, default=2)
    p.add_argument("--export")
    a = p.parse_args()
    export(a.export) if a.export else train(a)
