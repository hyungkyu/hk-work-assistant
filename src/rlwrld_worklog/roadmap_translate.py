"""Putting the roadmap into English and Japanese as it arrives.

The roadmap screen has had a language toggle since it was a generated file,
and until 2026-10-14 only the seed's 95 rows had anything behind it. A refresh
reads Korean from Notion and cannot translate, so every row it added or
reworded carried its Korean in all three columns and a "번역 대기" badge.
HK, 2026-10-14: the content itself has to be multilingual, not only the menu.

**Where it runs.** On this machine, with a local model. The same reason the
embeddings are local: the roadmap is the company's plans, and HK chose that
it does not leave the machine to be translated. The model is
`WORKLOG_TRANSLATE_MODEL`, by default a 4B instruction model that fits the
16GB card with room to spare.

**When.** After a refresh, not while the screen is open. `translated_hash` is
the Korean hash a row's English and Japanese were made from; a row whose hash
moved past it is pending, and only pending rows are translated. A batch picks
them up, so a refresh from the backoffice button -- which runs in the
container, away from the GPU -- is translated within one tick.

**What it refuses to write.** A translation that comes back empty, or still
carries Hangul, is not written. The row keeps its old text and its
"번역 대기" badge: English that reads as current and is not is worse than
English that says it is behind. And a row whose Korean changed again while
it was being translated is left for the next run rather than stamped current
with the wrong text.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

DEFAULT_MODEL = os.environ.get("WORKLOG_TRANSLATE_MODEL", "Qwen/Qwen3-4B-Instruct-2507")
BATCH = int(os.environ.get("WORKLOG_TRANSLATE_BATCH", "16"))
MAX_NEW_TOKENS = 320

LANGUAGES = {"en": "English", "ja": "Japanese"}

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
# Simplified-Chinese forms that Japanese never uses. The model slipped one into
# 実験 on the live page (实験, 2026-10-14); Hangul alone would have let it through.
SIMPLIFIED = re.compile(
    "[实验这们个发对时进关说过还应该经现动问题长开头书车东区产业务处变门间无号义议记设读请认话调资级线组织结给统继续总转较边达运连选适递"
    "钱铁闭险难风类质证]"
)

# Korean loanwords from robotics and ML that a small model reads wrongly. Found
# on the live page on 2026-10-14: 텍타일 came back as "textile", 핸드 as ケア,
# 학습 as "learning". Add to it when a translation shows another.
GLOSSARY = {
    "텍타일": ("tactile", "タクタイル"),
    "핸드": ("hand", "ハンド"),
    "휴머노이드": ("humanoid", "ヒューマノイド"),
    "학습": ("training", "学習"),
    "텔레오퍼레이션": ("teleoperation", "テレオペレーション"),
    "브링업": ("bring-up", "ブリングアップ"),
    "리더 디바이스": ("leader device", "リーダーデバイス"),
    "그리퍼": ("gripper", "グリッパー"),
    "지그": ("jig", "治具"),
    "파지력": ("grip force", "把持力"),
    "엄지": ("thumb", "親指"),
    "손마디": ("finger joint", "指関節"),
    "체크포인트": ("checkpoint", "チェックポイント"),
    "데이터셋": ("dataset", "データセット"),
    # The company's own name, written in Hangul. Came back as "Riwool World".
    "리월월드": ("RLWRLD", "RLWRLD"),
    # 전사 here is company-wide, not transcription ("Modal transfer usage").
    "전사": ("company-wide", "全社"),
    "실로봇": ("real robot", "実機ロボット"),
    "유휴": ("idle", "遊休"),
    "서빙": ("serving", "サービング"),
    "어뷰징": ("abuse", "不正利用"),
    "배포": ("deployment", "デプロイ"),
    "정착": ("adoption", "定着"),
    "손가락": ("finger", "指"),
    "손목": ("wrist", "手首"),
    "연내": ("within this year", "年内"),
}

SYSTEM = (
    "You translate one line of a Korean robotics and ML engineering roadmap into {language}. "
    "Keep every Latin-script word exactly as written: product names, code names, acronyms "
    "and English labels such as RRC, HRDexDB, IK, ALLEX, ALDEX, Foundry, Slurm, K8s, "
    "Fleet-Serve, Deploy, 'wheeled humanoid', 'HW Develop', 'HW Test'. Do not expand or "
    "translate them, and in Japanese do not turn them into katakana. "
    "Keep each ' — ' separator and the order of the parts around it. "
    "Use these terms: {glossary}. "
    "Translate the Korean words fully; leave no Korean in the output. "
    "Output only the translated line: no quotes, no notes, no explanation."
)

# One worked line, so the model sees the labels kept and the loanwords right.
EXAMPLE = "HW Develop — ALLEX 핸드 텍타일 센서 부착 및 학습 데이터 수집"
EXAMPLE_ANSWER = {
    "en": "HW Develop — Attach tactile sensors to the ALLEX hand and collect training data",
    "ja": "HW Develop — ALLEXハンドへのタクタイルセンサー取り付けと学習データ収集",
}


def _messages(text: str, language: str) -> list[dict[str, str]]:
    index = 0 if language == "en" else 1
    glossary = ", ".join(f"{ko} = {terms[index]}" for ko, terms in GLOSSARY.items())
    return [
        {"role": "system", "content": SYSTEM.format(language=LANGUAGES[language], glossary=glossary)},
        {"role": "user", "content": EXAMPLE},
        {"role": "assistant", "content": EXAMPLE_ANSWER[language]},
        {"role": "user", "content": text},
    ]


class Translator(Protocol):
    @property
    def name(self) -> str: ...

    def translate(self, texts: Sequence[str], language: str, *, retry: bool = False) -> list[str]: ...


def clean(output: str) -> str:
    """The first line of what the model said, without wrapping it added."""
    text = output.strip()
    for line in text.splitlines():
        if line.strip():
            text = line.strip()
            break
    for opening, closing in ('""', "''", "“”", "「」", "『』"):
        if len(text) >= 2 and text[0] == opening and text[-1] == closing:
            return text[1:-1].strip()
    return text


def acceptable(translation: str, language: str) -> bool:
    """Something to write: not empty, no Korean left, and Japanese that is Japanese."""
    if not translation.strip() or HANGUL.search(translation):
        return False
    return not (language == "ja" and SIMPLIFIED.search(translation))


class LocalTranslator:
    """An instruction model on this machine, loaded on first use.

    Loading takes seconds and a few GB of GPU memory, so it happens only when
    there is something to translate, and once per run.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._tokenizer: Any = None

    @property
    def name(self) -> str:
        return self._model_name

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as error:  # pragma: no cover - environment
            raise RuntimeError(
                "transformers and torch are needed to translate locally: "
                ".venv/bin/pip install transformers torch"
            ) from error
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name, padding_side="left")
        # Moved after loading rather than placed by `device_map`, which would
        # need `accelerate` for a model that fits one card whole.
        self._model = AutoModelForCausalLM.from_pretrained(self._model_name, dtype=torch.bfloat16)
        self._model.to("cuda" if torch.cuda.is_available() else "cpu")
        self._model.eval()

    def translate(self, texts: Sequence[str], language: str, *, retry: bool = False) -> list[str]:
        """Greedy the first time; a retry samples, since greedy would only say
        the same wrong thing again."""
        if not texts:
            return []
        self._load()
        import torch

        prompts = [
            self._tokenizer.apply_chat_template(
                _messages(text, language), tokenize=False, add_generation_prompt=True,
            )
            for text in texts
        ]
        batch = self._tokenizer(prompts, return_tensors="pt", padding=True).to(self._model.device)
        with torch.inference_mode():
            sampling = {"do_sample": True, "temperature": 0.4, "top_p": 0.9} if retry else {"do_sample": False}
            generated = self._model.generate(
                **batch, max_new_tokens=MAX_NEW_TOKENS,
                pad_token_id=self._tokenizer.pad_token_id, **sampling,
            )
        new = generated[:, batch["input_ids"].shape[1]:]
        return [clean(text) for text in self._tokenizer.batch_decode(new, skip_special_tokens=True)]


@dataclass
class Result:
    pending: int = 0
    translated: int = 0
    # Came back empty or still Korean; left pending for the next run.
    rejected: list[str] = field(default_factory=list)
    # The Korean moved while it was being translated; left for the next run.
    raced: list[str] = field(default_factory=list)
    # The rejections as item_key -> the hash they were refused at, for the
    # caller to remember.
    refused: dict[str, str] = field(default_factory=dict)
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "translated": self.translated,
            "rejected": len(self.rejected),
            "raced": len(self.raced),
            "model": self.model,
        }


PENDING_SQL = """
SELECT item_key, text_ko, hash FROM roadmap_item
WHERE translated_hash IS DISTINCT FROM hash
ORDER BY sort, id
"""

WRITE_SQL = """
UPDATE roadmap_item SET text_en = %s, text_ja = %s, translated_hash = hash
WHERE item_key = %s AND hash = %s
"""


def pending_rows(
    database_url: str, *, limit: int | None = None, skip: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Rows whose translation is behind, less any already refused at this hash."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(PENDING_SQL)
            columns = [column.name for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    skip = skip or {}
    rows = [row for row in rows if skip.get(str(row["item_key"])) != str(row["hash"])]
    return rows[:limit] if limit else rows


def _translate_one_language(texts: list[str], language: str, translator: Translator) -> list[str]:
    """One pass, then one sampled retry for what came back unfit."""
    out = translator.translate(texts, language)
    again = [n for n, text in enumerate(out) if not acceptable(text, language)]
    if again:
        retried = translator.translate([texts[n] for n in again], language, retry=True)
        for n, text in zip(again, retried):
            out[n] = text
    return out


def translate_rows(
    rows: Sequence[Mapping[str, Any]], translator: Translator
) -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str]]]:
    """Each row's English and Japanese, and the rows that could not be written.

    Pure apart from the translator, so it is tested with a fake one.
    Returns (writes, rejected): a write is (en, ja, item_key, hash) and a
    rejection is (item_key, hash).
    """
    writes: list[tuple[str, str, str, str]] = []
    rejected: list[tuple[str, str]] = []
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        texts = [str(row["text_ko"]) for row in chunk]
        english = _translate_one_language(texts, "en", translator)
        japanese = _translate_one_language(texts, "ja", translator)
        for row, en, ja in zip(chunk, english, japanese):
            key, digest = str(row["item_key"]), str(row["hash"])
            if acceptable(en, "en") and acceptable(ja, "ja"):
                writes.append((en, ja, key, digest))
            else:
                rejected.append((key, digest))
    return writes, rejected


def translate_pending(
    database_url: str,
    translator: Translator,
    *,
    limit: int | None = None,
    apply: bool = True,
    skip: Mapping[str, str] | None = None,
) -> Result:
    """Translate what is pending and write what is fit to write.

    Nothing pending means the model is never loaded: the batch runs every few
    minutes and almost every run has nothing to do. `skip` holds rows refused
    before at the same Korean, so a line the model cannot do does not reload
    the model every tick to fail again; a change to its Korean retries it.
    """
    import psycopg

    rows = pending_rows(database_url, limit=limit, skip=skip)
    result = Result(pending=len(rows), model=translator.name)
    if not rows or not apply:
        return result
    writes, rejected = translate_rows(rows, translator)
    result.rejected = [key for key, _ in rejected]
    result.refused = dict(rejected)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for en, ja, key, digest in writes:
                cursor.execute(WRITE_SQL, (en, ja, key, digest))
                if cursor.rowcount:
                    result.translated += 1
                else:
                    result.raced.append(key)
        connection.commit()
    return result
