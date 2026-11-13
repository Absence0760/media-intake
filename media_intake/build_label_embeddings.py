#!/usr/bin/env python3
"""Regenerate media_intake/labels.npz (SigLIP text embeddings of labels.yaml's prompts).

  bash setup.sh --build-labels     (fetches what this needs, then runs it)
  venv/bin/python -I media_intake/build_label_embeddings.py BUILD_DIR

Only when labels.yaml changes; the runtime (label.py) needs just the vision
encoder and this file. BUILD_DIR holds text_model.onnx and tokenizer.json from
Xenova/siglip-base-patch16-224 at the pinned revision (sha256-checked by setup.sh)
and site/ with the hash-pinned tokenizers wheel (requirements-labels.txt).

Text handling follows transformers' SiglipTokenizer: lower case, punctuation
removed, whitespace collapsed, </s> appended, padded with </s> (id 1) to 64
tokens (SigLIP was trained on max_length padding).
"""
import os, re, string, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_LEN, PAD = 64, 1


def canonical(t):
    t = t.lower().translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", t).strip()


def main():
    build = os.path.abspath(sys.argv[1])
    sys.path.insert(0, os.path.join(build, "site"))
    sys.path.insert(1, HERE)
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    import label
    _, prompts, _, _, _ = label.label_config()
    tok = Tokenizer.from_file(os.path.join(build, "tokenizer.json"))
    ids = np.full((len(prompts), MAX_LEN), PAD, dtype=np.int64)
    for i, p in enumerate(prompts):
        if canonical(p) != p:
            sys.exit(f"labels.yaml: write prompts lower case without punctuation: {p!r}")
        e = tok.encode(p).ids[:MAX_LEN]
        if e[-1] != 1:
            e[-1:] = [1]
        ids[i, :len(e)] = e
    sess = ort.InferenceSession(os.path.join(build, "text_model.onnx"), providers=["CPUExecutionProvider"])
    feed = {"input_ids": ids}
    if any(i.name == "attention_mask" for i in sess.get_inputs()):
        feed["attention_mask"] = np.ones_like(ids)
    emb = sess.run(["pooler_output"], feed)[0].astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    out = os.path.join(HERE, "labels.npz")
    np.savez(out, emb=emb, prompts=np.array(prompts))
    print(f"wrote {out}: {len(prompts)} prompts x {emb.shape[1]}")


if __name__ == "__main__":
    main()
