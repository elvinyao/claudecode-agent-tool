"""Versioned Japanese junior-exam card-authoring policy."""

from __future__ import annotations

from ankify.models import AnkifyRunOptions, JuniorExamStage, JuniorExamSubject
from ankify.strategies.base import stable_tags

STRATEGY_VERSION = "junior-exam-v1"

_SUBJECT_RULES: dict[JuniorExamSubject, tuple[str, tuple[str, ...]]] = {
    JuniorExamSubject.KOKUGO: (
        "kokugo",
        (
            "国語では語句の一言まとめ、漢字・読み、記述の書き出し型、心情の根拠、選択肢の消去法、段落構造を重視する。",
            "長い添削文ではなく、反復可能な短い観点・型・根拠確認カードに分ける。",
        ),
    ),
    JuniorExamSubject.SANSUU: (
        "sansuu",
        (
            "算数では条件整理、なぜその式か、解法の入口、よくあるつまずき、次に確認する行動を重視する。",
            "一枚に完全な長い解説を詰めず、一つの条件・理由・ミス・最初の一手へ分ける。",
        ),
    ),
    JuniorExamSubject.RIKA: (
        "rika",
        (
            "理科では用語、実験の目的・手順・理由、観察結果、公式を使う条件、紛らわしい用語比較を重視する。",
            "資料にない実験結果、最新知識、数値を追加しない。",
        ),
    ),
    JuniorExamSubject.SHAKAI: (
        "shakai",
        (
            "社会では人物、事件、年代、制度、前後の違い、因果関係、地理的要因と結果を分けて問う。",
            "時事問題や統計はモデルの記憶だけで作らず、入力資料にない事実を断定しない。",
        ),
    ),
}

_STAGE_RULES: dict[JuniorExamStage, tuple[str, str]] = {
    JuniorExamStage.GRADE4: (
        "grade4",
        "小4にも理解できる短い表現を使い、基礎語彙と基本パターンを優先する。",
    ),
    JuniorExamStage.GRADE5: (
        "grade5",
        "小5向けに、基礎から標準問題へつながる考え方を重視する。",
    ),
    JuniorExamStage.GRADE6: (
        "grade6",
        "小6向けに、入試で問われやすい比較・因果・典型パターンを重視する。",
    ),
    JuniorExamStage.FINAL_PUSH: (
        "final-push",
        "直前期向けに、頻出事項、間違えやすい点、短時間で見直せるカードを優先する。",
    ),
}


def junior_exam_policy(options: AnkifyRunOptions) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if options.exam_subject is None or options.exam_stage is None:
        raise ValueError("junior-exam strategy requires subject and stage")
    subject_tag, subject_rules = _SUBJECT_RULES[options.exam_subject]
    stage_tag, stage_rule = _STAGE_RULES[options.exam_stage]
    source_tag = {
        "materials_notes": "materials",
        "mistakes_explanations": "mistake-review",
        "topic_scope": "topic-scope",
    }[options.source_mode.value]
    tags = stable_tags(
        ("chugaku-juken", subject_tag, stage_tag, source_tag),
        options.user_tags,
    )
    rules = (
        *subject_rules,
        stage_rule,
        "カード本文は日本語とし、Frontは短く明確な問い、Backは簡潔な答えにする。",
        "各カードは一つの知識点だけを扱い、独立した知識点は分割する。",
        "中学受験 profile ではBasicカードだけを生成する。Cloze記法を使わない。",
    )
    return tags, rules


__all__ = ["STRATEGY_VERSION", "junior_exam_policy"]
