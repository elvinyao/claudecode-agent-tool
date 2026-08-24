# Junior Exam Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Japanese 中学受験 guided deck creation flow with four-subject strategies, Basic-only generation, and app-only quality review flags.

**Architecture:** Keep the current generic Ankify flow intact and add a specialized metadata layer for study purpose, subject, stage, source mode, and quality flags. The guided UI submits to the existing `/api/jobs` endpoint with new form fields; backend persistence, prompt construction, LLM schema, and review UI all consume those fields.

**Tech Stack:** Next.js App Router, React client components, TypeScript, sql.js, Vitest, OpenAI/Gemini structured JSON generation.

---

## File Structure

- `lib/types.ts`: Add shared union types for study purpose, junior exam subject, stage, source mode, and quality flags; extend job/card records and creation/patch inputs.
- `lib/junior-exam.ts`: New focused strategy module. It maps subject/stage/source mode to Japanese UI labels, tags, subject summaries, and prompt strategy text.
- `lib/db.ts`: Add schema columns, auto-migration, mapper fields, inserts, updates, and card quality flag persistence.
- `lib/llm-schema.ts`: Add `quality_flags` to structured LLM output for cards.
- `lib/llm-prompts.ts`: Thread job metadata into draft/review prompts and include junior exam strategy text only for `studyPurpose === "junior_exam"`.
- `lib/llm-provider.ts`: Normalize `quality_flags` and keep them separate from Anki tags.
- `app/api/jobs/route.ts`: Parse new form fields, validate 中学受験 requirements, force Basic cards for 中学受験, and create jobs with metadata.
- `components/purpose-selection.tsx`: New Japanese purpose selector for the home page.
- `components/junior-exam-create-form.tsx`: New 3-step Japanese guided form.
- `app/page.tsx`: Replace the existing home composition with purpose selection, generic create form, or junior exam guide depending on query param.
- `components/job-review.tsx`: Add quality summary and filter for 要確認 cards.
- `components/review-card-editor.tsx`: Render quality labels on cards.
- `app/globals.css`: Add layout and badge classes for purpose cards, guide steps, and quality flags.
- Tests under `__tests__/`: Extend DB/API/schema/prompt/export coverage.

## Task 1: Add Domain Types and Junior Exam Strategy Module

**Files:**
- Modify: `lib/types.ts`
- Create: `lib/junior-exam.ts`
- Test: `__tests__/junior-exam.test.ts`

- [ ] **Step 1: Write the failing strategy tests**

Add `__tests__/junior-exam.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import {
  getJuniorExamStrategy,
  isJuniorExamStage,
  isJuniorExamSubject,
  isSourceMode,
} from "@/lib/junior-exam";

describe("junior exam strategy", () => {
  it("validates known subject, stage, and source mode values", () => {
    expect(isJuniorExamSubject("sansuu")).toBe(true);
    expect(isJuniorExamSubject("english")).toBe(false);
    expect(isJuniorExamStage("grade5")).toBe(true);
    expect(isJuniorExamStage("junior_high")).toBe(false);
    expect(isSourceMode("mistakes_explanations")).toBe(true);
    expect(isSourceMode("random")).toBe(false);
  });

  it("builds a math mistake strategy with solving-method language", () => {
    const strategy = getJuniorExamStrategy({
      subject: "sansuu",
      stage: "grade5",
      sourceMode: "mistakes_explanations",
    });

    expect(strategy.subjectLabel).toBe("算数");
    expect(strategy.stageLabel).toBe("小5");
    expect(strategy.defaultTags).toEqual(["chugaku-juken", "sansuu", "grade5", "mistake-review"]);
    expect(strategy.promptText).toContain("なぜその式か");
    expect(strategy.promptText).toContain("つまずき原因");
    expect(strategy.promptText).toContain("一枚のカードに完全な解説を詰め込まない");
  });

  it("builds a social studies strategy without current-affairs generation from memory", () => {
    const strategy = getJuniorExamStrategy({
      subject: "shakai",
      stage: "grade6",
      sourceMode: "materials_notes",
    });

    expect(strategy.subjectLabel).toBe("社会");
    expect(strategy.promptText).toContain("地理的要因");
    expect(strategy.promptText).toContain("時事問題はモデルの記憶だけで作らない");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- __tests__/junior-exam.test.ts`

Expected: FAIL because `lib/junior-exam.ts` does not exist.

- [ ] **Step 3: Add the shared types**

In `lib/types.ts`, add these exports near the existing union types:

```ts
export type StudyPurpose = "junior_exam" | "language" | "exam_prep" | "free";
export type JuniorExamSubject = "kokugo" | "sansuu" | "rika" | "shakai";
export type JuniorExamStage = "grade4" | "grade5" | "grade6" | "final_push";
export type SourceMode = "materials_notes" | "mistakes_explanations" | "topic_scope";
export type QualityFlag =
  | "too_long"
  | "multi_point"
  | "source_check"
  | "strategy_mismatch"
  | "solution_gap"
  | "low_confidence";
```

Extend `JobRecord`, `CreateJobRecordInput`, and `JobPatch`:

```ts
studyPurpose: StudyPurpose;
examSubject: JuniorExamSubject | null;
examStage: JuniorExamStage | null;
sourceMode: SourceMode;
```

Extend `CardRecord`, `CardPatch`, `GeneratedCard`, and `StructuredLlmCard`:

```ts
qualityFlags: QualityFlag[];
```

For `StructuredLlmCard`, use JSON field naming:

```ts
quality_flags: QualityFlag[];
```

- [ ] **Step 4: Add `lib/junior-exam.ts`**

Create `lib/junior-exam.ts`:

```ts
import type { JuniorExamStage, JuniorExamSubject, SourceMode } from "@/lib/types";

export const JUNIOR_EXAM_SUBJECTS: Record<
  JuniorExamSubject,
  { label: string; summary: string; baseTags: string[]; strategy: string }
> = {
  kokugo: {
    label: "国語",
    summary: "語句・漢字・読解の要点",
    baseTags: ["kokugo"],
    strategy: [
      "国語では語句の一言まとめ、漢字・読み、記述の書き出し型、心情読み取り、本文根拠の探し方、選択肢の消去法、段落構造を重視する。",
      "記述添削そのものではなく、復習用カードとして使える短い型・観点・根拠確認に変換する。",
    ].join(" "),
  },
  sansuu: {
    label: "算数",
    summary: "公式・条件・解法の入口",
    baseTags: ["sansuu"],
    strategy: [
      "算数では文章題の条件整理、なぜその式か、図形の補助線ヒント、速さ・割合・比・場合の数、計算ミス、単元要点を重視する。",
      "公式の丸暗記ではなく、いつ使うか、なぜその式になるか、最初に注目する条件、よくあるミスをカード化する。",
      "一枚のカードに完全な解説を詰め込まない。長いステップ解説は短い記憶カードと少量の解法の入口カードに分ける。",
    ].join(" "),
  },
  rika: {
    label: "理科",
    summary: "用語・実験・理由",
    baseTags: ["rika"],
    strategy: [
      "理科では用語、実験の目的・手順・理由、手順を省いた場合、観察結果からわかること、公式を使う場面、紛らわしい用語比較、単元横断を重視する。",
      "語呂合わせは暗記補助として使えるが、毎回強制しない。",
    ].join(" "),
  },
  shakai: {
    label: "社会",
    summary: "人物・事件・因果・年代",
    baseTags: ["shakai"],
    strategy: [
      "社会では人物、事件、年代、地名、制度、前の時代との違い、因果関係、地理的要因 -> 歴史的結果、統計データ読み取りを重視する。",
      "時事問題はモデルの記憶だけで作らない。資料にない最新ニュースや統計を断定的に追加しない。",
    ].join(" "),
  },
};

export const JUNIOR_EXAM_STAGES: Record<JuniorExamStage, { label: string; tag: string; guidance: string }> = {
  grade4: { label: "小4", tag: "grade4", guidance: "小4にもわかる短い表現を使い、基礎語彙と基本パターンを優先する。" },
  grade5: { label: "小5", tag: "grade5", guidance: "小5向けに、基礎から標準問題へつながる考え方を重視する。" },
  grade6: { label: "小6", tag: "grade6", guidance: "小6向けに、入試で問われやすい比較・因果・典型パターンを重視する。" },
  final_push: { label: "直前期", tag: "final-push", guidance: "直前期向けに、頻出事項、間違えやすい点、短時間で見直せるカードを優先する。" },
};

export const SOURCE_MODES: Record<SourceMode, { label: string; tag: string; guidance: string }> = {
  materials_notes: {
    label: "教材・講義・ノート",
    tag: "materials",
    guidance: "資料に含まれる知識点を忠実に、短い復習カードへ変換する。",
  },
  mistakes_explanations: {
    label: "間違えた問題・解説",
    tag: "mistake-review",
    guidance: "つまずき原因、正しい考え方、次に同じミスを防ぐ観点を優先する。",
  },
  topic_scope: {
    label: "範囲・テーマ",
    tag: "topic-scope",
    guidance: "テーマに沿って中学受験で復習価値の高い基本事項を作る。最新時事は資料がない限り作らない。",
  },
};

export function isJuniorExamSubject(value: string): value is JuniorExamSubject {
  return value === "kokugo" || value === "sansuu" || value === "rika" || value === "shakai";
}

export function isJuniorExamStage(value: string): value is JuniorExamStage {
  return value === "grade4" || value === "grade5" || value === "grade6" || value === "final_push";
}

export function isSourceMode(value: string): value is SourceMode {
  return value === "materials_notes" || value === "mistakes_explanations" || value === "topic_scope";
}

export function getJuniorExamStrategy(params: {
  subject: JuniorExamSubject;
  stage: JuniorExamStage;
  sourceMode: SourceMode;
}) {
  const subject = JUNIOR_EXAM_SUBJECTS[params.subject];
  const stage = JUNIOR_EXAM_STAGES[params.stage];
  const sourceMode = SOURCE_MODES[params.sourceMode];

  return {
    subjectLabel: subject.label,
    stageLabel: stage.label,
    sourceModeLabel: sourceMode.label,
    summary: subject.summary,
    defaultTags: ["chugaku-juken", ...subject.baseTags, stage.tag, sourceMode.tag],
    promptText: [
      `中学受験${subject.label}のカードを作る。対象は${stage.label}。資料タイプは「${sourceMode.label}」。`,
      subject.strategy,
      stage.guidance,
      sourceMode.guidance,
      "カード本文は日本語。Frontは短く明確な問いにする。Backは簡潔にする。",
      "各カードは一つの知識点だけを扱う。複数の独立した知識点がある場合は分割する。",
      "Anki用なので、長い家庭教師の解説をそのまま入れず、反復しやすい短いカードに変換する。",
    ].join(" "),
  };
}
```

- [ ] **Step 5: Run the strategy tests**

Run: `npm test -- __tests__/junior-exam.test.ts`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lib/types.ts lib/junior-exam.ts __tests__/junior-exam.test.ts
git commit -m "feat: add junior exam strategy definitions"
```

## Task 2: Persist Study Metadata and Quality Flags

**Files:**
- Modify: `lib/db.ts`
- Test: `__tests__/db.test.ts`

- [ ] **Step 1: Write failing DB metadata tests**

Add to `__tests__/db.test.ts`:

```ts
it("persists junior exam job metadata", async () => {
  const job = await createJobRecord({
    id: "job_junior",
    inputType: "prompt",
    cardType: "basic",
    studyPurpose: "junior_exam",
    examSubject: "sansuu",
    examStage: "grade5",
    sourceMode: "mistakes_explanations",
    deckName: "算数",
    requestedCardCount: 10,
    language: "ja",
    aiInstructions: "",
    userTags: ["chugaku-juken"],
    sources: [],
  });

  expect(job.studyPurpose).toBe("junior_exam");
  expect(job.examSubject).toBe("sansuu");
  expect(job.examStage).toBe("grade5");
  expect(job.sourceMode).toBe("mistakes_explanations");
});

it("persists generated and updated card quality flags", async () => {
  await createJobRecord({
    id: "job_quality",
    inputType: "prompt",
    cardType: "basic",
    studyPurpose: "junior_exam",
    examSubject: "rika",
    examStage: "grade6",
    sourceMode: "materials_notes",
    deckName: "理科",
    requestedCardCount: 10,
    language: "ja",
    aiInstructions: "",
    userTags: [],
    sources: [],
  });

  await replaceGeneratedCards("job_quality", [
    {
      front: "実験で最初に確認することは？",
      back: "目的と条件。",
      tags: ["rika"],
      sourceRef: "notes",
      confidence: 0.7,
      qualityFlags: ["source_check", "solution_gap"],
    },
  ]);

  const cards = await listCards("job_quality");
  expect(cards[0].qualityFlags).toEqual(["source_check", "solution_gap"]);

  const updated = await updateCard(cards[0].id, { qualityFlags: ["too_long"] });
  expect(updated?.qualityFlags).toEqual(["too_long"]);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- __tests__/db.test.ts`

Expected: FAIL because the DB schema and mappers do not persist the new fields.

- [ ] **Step 3: Update SQL schema and migrations**

In `lib/db.ts`, extend `jobs`:

```sql
study_purpose TEXT NOT NULL DEFAULT 'free',
exam_subject TEXT,
exam_stage TEXT,
source_mode TEXT NOT NULL DEFAULT 'materials_notes',
```

Extend `cards`:

```sql
quality_flags_json TEXT NOT NULL DEFAULT '[]',
```

Replace the current one-off `card_type` migration block with reusable column checks:

```ts
function tableHasColumn(db: Database, tableName: string, columnName: string) {
  const tableInfo = db.exec(`PRAGMA table_info(${tableName})`);
  return tableInfo.length > 0 && tableInfo[0].values.some((col) => col[1] === columnName);
}

function addColumnIfMissing(db: Database, tableName: string, columnName: string, sql: string) {
  if (!tableHasColumn(db, tableName, columnName)) {
    db.exec(`ALTER TABLE ${tableName} ADD COLUMN ${sql}`);
  }
}
```

Call after `db.exec(SCHEMA_SQL)` for non-first load:

```ts
addColumnIfMissing(db, "jobs", "card_type", "card_type TEXT NOT NULL DEFAULT 'basic'");
addColumnIfMissing(db, "jobs", "study_purpose", "study_purpose TEXT NOT NULL DEFAULT 'free'");
addColumnIfMissing(db, "jobs", "exam_subject", "exam_subject TEXT");
addColumnIfMissing(db, "jobs", "exam_stage", "exam_stage TEXT");
addColumnIfMissing(db, "jobs", "source_mode", "source_mode TEXT NOT NULL DEFAULT 'materials_notes'");
addColumnIfMissing(db, "cards", "quality_flags_json", "quality_flags_json TEXT NOT NULL DEFAULT '[]'");
```

- [ ] **Step 4: Update DB mappers and inserts**

In `mapJob`, add:

```ts
studyPurpose: String(row.study_purpose ?? "free") as JobRecord["studyPurpose"],
examSubject: row.exam_subject ? (String(row.exam_subject) as JobRecord["examSubject"]) : null,
examStage: row.exam_stage ? (String(row.exam_stage) as JobRecord["examStage"]) : null,
sourceMode: String(row.source_mode ?? "materials_notes") as JobRecord["sourceMode"],
```

In `mapCard`, add:

```ts
qualityFlags: parseJsonArray(row.quality_flags_json).map(String) as CardRecord["qualityFlags"],
```

In `createJobRecord`, include `study_purpose, exam_subject, exam_stage, source_mode` in the INSERT and bind:

```ts
$studyPurpose: input.studyPurpose,
$examSubject: input.examSubject,
$examStage: input.examStage,
$sourceMode: input.sourceMode,
```

In `replaceGeneratedCards`, `createCard`, and `updateCard`, include `quality_flags_json` and bind JSON arrays. Manual cards use `[]`.

- [ ] **Step 5: Run DB tests**

Run: `npm test -- __tests__/db.test.ts`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lib/db.ts __tests__/db.test.ts
git commit -m "feat: persist study metadata and quality flags"
```

## Task 3: Extend LLM Schema, Normalization, and Prompt Strategy

**Files:**
- Modify: `lib/llm-schema.ts`
- Modify: `lib/llm-provider.ts`
- Modify: `lib/llm-prompts.ts`
- Test: `__tests__/llm-schema.test.ts`
- Test: `__tests__/llm-provider.test.ts`
- Test: `__tests__/llm-prompts.test.ts`

- [ ] **Step 1: Write failing schema and prompt tests**

Add to `__tests__/llm-schema.test.ts`:

```ts
it("accepts quality_flags in generated cards", () => {
  const result = GeneratedBatchSchema.parse({
    cards: [
      {
        front: "Q",
        back: "A",
        tags: ["sansuu"],
        source_ref: "source",
        confidence: 0.6,
        quality_flags: ["source_check", "solution_gap"],
      },
    ],
  });

  expect(result.cards[0].quality_flags).toEqual(["source_check", "solution_gap"]);
});

it("defaults missing quality_flags to an empty array", () => {
  const result = GeneratedBatchSchema.parse({
    cards: [
      {
        front: "Q",
        back: "A",
        tags: [],
        source_ref: null,
        confidence: 0.6,
      },
    ],
  });

  expect(result.cards[0].quality_flags).toEqual([]);
});
```

Create `__tests__/llm-prompts.test.ts` if it does not exist:

```ts
import { describe, expect, it } from "vitest";

import { buildDraftDeveloperPrompt, buildReviewDeveloperPrompt } from "@/lib/llm-prompts";
import type { JobRecord } from "@/lib/types";

function makeJob(overrides: Partial<JobRecord> = {}): JobRecord {
  return {
    id: "job_1",
    status: "queued",
    inputType: "prompt",
    cardType: "basic",
    studyPurpose: "junior_exam",
    examSubject: "sansuu",
    examStage: "grade5",
    sourceMode: "mistakes_explanations",
    deckName: "算数",
    requestedCardCount: 10,
    language: "ja",
    aiInstructions: "",
    userTags: [],
    errorMessage: null,
    createdAt: "2026-05-21T00:00:00.000Z",
    updatedAt: "2026-05-21T00:00:00.000Z",
    completedAt: null,
    sourceCount: 1,
    cardCount: 0,
    artifactId: null,
    artifactFormat: null,
    artifactPath: null,
    ...overrides,
  };
}

describe("llm prompts", () => {
  it("includes junior exam strategy in draft prompts", () => {
    const prompt = buildDraftDeveloperPrompt(makeJob());
    expect(prompt).toContain("中学受験算数");
    expect(prompt).toContain("なぜその式か");
    expect(prompt).toContain("quality_flags");
  });

  it("does not include junior exam strategy for generic jobs", () => {
    const prompt = buildDraftDeveloperPrompt(
      makeJob({ studyPurpose: "free", examSubject: null, examStage: null }),
    );
    expect(prompt).not.toContain("中学受験算数");
  });

  it("asks review prompts to produce internal quality flags", () => {
    const prompt = buildReviewDeveloperPrompt(makeJob());
    expect(prompt).toContain("要確認");
    expect(prompt).toContain("quality_flags");
  });
});
```

- [ ] **Step 2: Run tests to verify failure**

Run: `npm test -- __tests__/llm-schema.test.ts __tests__/llm-prompts.test.ts`

Expected: FAIL because prompt function signatures and schemas are not updated.

- [ ] **Step 3: Update LLM schema**

In `GeneratedBatchSchema`, add:

```ts
quality_flags: z.array(
  z.enum(["too_long", "multi_point", "source_check", "strategy_mismatch", "solution_gap", "low_confidence"]),
).default([]),
```

In `generatedBatchJsonSchema.items.properties`, add:

```ts
quality_flags: {
  type: "array",
  items: {
    type: "string",
    enum: ["too_long", "multi_point", "source_check", "strategy_mismatch", "solution_gap", "low_confidence"],
  },
  description: "Internal app-only quality flags for cards that need parent confirmation. Use [] for normal cards.",
},
```

Include `"quality_flags"` in required fields for Gemini JSON schema.

- [ ] **Step 4: Update prompt builder signatures**

Change calls from:

```ts
buildDraftDeveloperPrompt(inputType, cardType)
buildReviewDeveloperPrompt(inputType, cardType)
```

to:

```ts
buildDraftDeveloperPrompt(job)
buildReviewDeveloperPrompt(job)
```

In `lib/llm-prompts.ts`, import `getJuniorExamStrategy` and add:

```ts
function buildJuniorExamStrategyPrompt(job: JobRecord) {
  if (
    job.studyPurpose !== "junior_exam" ||
    !job.examSubject ||
    !job.examStage
  ) {
    return "";
  }

  return getJuniorExamStrategy({
    subject: job.examSubject,
    stage: job.examStage,
    sourceMode: job.sourceMode,
  }).promptText;
}
```

Append this strategy to both draft and review developer prompts when non-empty. Also add:

```ts
"Use quality_flags only for app review. Do not put quality issues into tags.",
"Set quality_flags to [] for normal cards. Use source_check, too_long, multi_point, strategy_mismatch, solution_gap, or low_confidence only when parent confirmation is useful.",
```

- [ ] **Step 5: Update provider normalization**

In `normalizeGeneratedCards`, include:

```ts
qualityFlags: card.quality_flags ?? [],
```

Update OpenAI/Gemini callers to pass the full `job` to prompt builders.

- [ ] **Step 6: Run focused tests**

Run: `npm test -- __tests__/llm-schema.test.ts __tests__/llm-provider.test.ts __tests__/llm-prompts.test.ts`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lib/llm-schema.ts lib/llm-provider.ts lib/llm-prompts.ts __tests__/llm-schema.test.ts __tests__/llm-provider.test.ts __tests__/llm-prompts.test.ts
git commit -m "feat: add junior exam prompt strategy and quality schema"
```

## Task 4: Parse and Validate Guided Job Creation

**Files:**
- Modify: `app/api/jobs/route.ts`
- Test: `__tests__/api-jobs.test.ts`

- [ ] **Step 1: Write failing API tests**

Add to `__tests__/api-jobs.test.ts`:

```ts
it("creates a junior exam job with required metadata and forced basic card type", async () => {
  const formData = new FormData();
  formData.append("studyPurpose", "junior_exam");
  formData.append("examSubject", "sansuu");
  formData.append("examStage", "grade5");
  formData.append("sourceMode", "mistakes_explanations");
  formData.append("inputType", "prompt");
  formData.append("cardType", "cloze");
  formData.append("promptText", "割合のつまずき");
  formData.append("deckName", "算数 小5");

  const mockJob = { id: "job_junior", status: "queued" };
  vi.mocked(createJobRecord).mockResolvedValue(mockJob as any);

  const response = await POST(new Request("http://localhost/api/jobs", { method: "POST", body: formData }));

  expect(response.status).toBe(201);
  expect(createJobRecord).toHaveBeenCalledWith(expect.objectContaining({
    studyPurpose: "junior_exam",
    examSubject: "sansuu",
    examStage: "grade5",
    sourceMode: "mistakes_explanations",
    cardType: "basic",
    language: "ja",
    userTags: expect.arrayContaining(["chugaku-juken", "sansuu", "grade5", "mistake-review"]),
  }));
});

it("rejects junior exam jobs without subject and stage", async () => {
  const formData = new FormData();
  formData.append("studyPurpose", "junior_exam");
  formData.append("inputType", "prompt");
  formData.append("promptText", "理科");

  const response = await POST(new Request("http://localhost/api/jobs", { method: "POST", body: formData }));
  const data = await response.json();

  expect(response.status).toBe(400);
  expect(data.error).toBe("中学受験では科目と段階を選択してください。");
});
```

- [ ] **Step 2: Run API tests to verify failure**

Run: `npm test -- __tests__/api-jobs.test.ts`

Expected: FAIL because the endpoint ignores the new fields.

- [ ] **Step 3: Parse and validate metadata**

In `app/api/jobs/route.ts`, import:

```ts
import {
  getJuniorExamStrategy,
  isJuniorExamStage,
  isJuniorExamSubject,
  isSourceMode,
} from "@/lib/junior-exam";
import type { CreatedJobSourceInput, InputType, StudyPurpose } from "@/lib/types";
```

Add:

```ts
function isStudyPurpose(value: string): value is StudyPurpose {
  return value === "junior_exam" || value === "language" || value === "exam_prep" || value === "free";
}
```

After reading `formData`, parse:

```ts
const rawStudyPurpose = String(formData.get("studyPurpose") ?? "free");
const studyPurpose = isStudyPurpose(rawStudyPurpose) ? rawStudyPurpose : "free";
const rawExamSubject = String(formData.get("examSubject") ?? "");
const rawExamStage = String(formData.get("examStage") ?? "");
const rawSourceMode = String(formData.get("sourceMode") ?? "materials_notes");
const sourceMode = isSourceMode(rawSourceMode) ? rawSourceMode : "materials_notes";
const examSubject = isJuniorExamSubject(rawExamSubject) ? rawExamSubject : null;
const examStage = isJuniorExamStage(rawExamStage) ? rawExamStage : null;

if (studyPurpose === "junior_exam" && (!examSubject || !examStage)) {
  return NextResponse.json(
    { error: "中学受験では科目と段階を選択してください。" },
    { status: 400 },
  );
}
```

Resolve card type:

```ts
const resolvedCardType = studyPurpose === "junior_exam" ? "basic" : cardType;
```

Resolve language:

```ts
const resolvedLanguage = studyPurpose === "junior_exam" ? "ja" : language;
```

Merge default tags:

```ts
const juniorExamTags =
  studyPurpose === "junior_exam" && examSubject && examStage
    ? getJuniorExamStrategy({ subject: examSubject, stage: examStage, sourceMode }).defaultTags
    : [];
const mergedUserTags = Array.from(new Set([...juniorExamTags, ...userTags]));
```

Pass the new fields to `createJobRecord`.

- [ ] **Step 4: Run API tests**

Run: `npm test -- __tests__/api-jobs.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/jobs/route.ts __tests__/api-jobs.test.ts
git commit -m "feat: accept junior exam job metadata"
```

## Task 5: Build Purpose Selection and Guided Form UI

**Files:**
- Create: `components/purpose-selection.tsx`
- Create: `components/junior-exam-create-form.tsx`
- Modify: `app/page.tsx`
- Modify: `app/globals.css`

- [ ] **Step 1: Add the purpose selector**

Create `components/purpose-selection.tsx`:

```tsx
import Link from "next/link";

const PURPOSES = [
  { href: "/?purpose=junior_exam", title: "中学受験", body: "国語・算数・理科・社会の復習カードをガイドに沿って作成します。" },
  { href: "/?purpose=language", title: "語学", body: "単語や文法など、自由作成フォームでカードを作ります。" },
  { href: "/?purpose=exam_prep", title: "試験対策", body: "試験範囲やノートから、自由作成フォームでカードを作ります。" },
  { href: "/?purpose=free", title: "自由作成", body: "既存のAnkiカード作成フローを使います。" },
] as const;

export function PurposeSelection() {
  return (
    <section className="purpose-shell anim-fade-up">
      <div className="purpose-head">
        <span className="eyebrow">Anki deck builder</span>
        <h1 className="page-title">学習目的を選んでください</h1>
        <p className="page-lead">最初に用途を選ぶと、カードの作り方を学習場面に合わせられます。</p>
      </div>
      <div className="purpose-grid">
        {PURPOSES.map((purpose) => (
          <Link key={purpose.href} className="purpose-card" href={purpose.href}>
            <h2>{purpose.title}</h2>
            <p>{purpose.body}</p>
          </Link>
        ))}
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Add the guided form**

Create `components/junior-exam-create-form.tsx`. Reuse file upload, prompt, and pasted text behavior from `CreateDeckForm`, but remove Cloze and AI Instructions controls. The submitted `FormData` must include:

```ts
payload.append("studyPurpose", "junior_exam");
payload.append("examSubject", subject);
payload.append("examStage", stage);
payload.append("sourceMode", sourceMode);
payload.append("inputType", inputType);
payload.append("cardType", "basic");
payload.append("language", "ja");
```

Use these local options:

```ts
const SUBJECT_OPTIONS = [
  { value: "kokugo", label: "国語" },
  { value: "sansuu", label: "算数" },
  { value: "rika", label: "理科" },
  { value: "shakai", label: "社会" },
] as const;

const STAGE_OPTIONS = [
  { value: "grade4", label: "小4" },
  { value: "grade5", label: "小5" },
  { value: "grade6", label: "小6" },
  { value: "final_push", label: "直前期" },
] as const;
```

Import `getJuniorExamStrategy` to show the selected subject summary:

```tsx
const strategy = getJuniorExamStrategy({ subject, stage, sourceMode });
```

Render three sections with step labels:

```tsx
<section className="guide-step">
  <span className="guide-step__label">Step 1</span>
  <h2>科目と段階</h2>
</section>
```

- [ ] **Step 3: Route the home page by query param**

Change `app/page.tsx` to:

```tsx
import { CreateDeckForm } from "@/components/create-deck-form";
import { JuniorExamCreateForm } from "@/components/junior-exam-create-form";
import { PurposeSelection } from "@/components/purpose-selection";

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<{ purpose?: string }>;
}) {
  const { purpose } = await searchParams;

  if (purpose === "junior_exam") {
    return <JuniorExamCreateForm />;
  }

  if (purpose === "language" || purpose === "exam_prep" || purpose === "free") {
    return (
      <div className="hero-layout">
        <CreateDeckForm />
        <section className="hero-copy anim-fade-up-1">
          <span className="eyebrow">自由作成</span>
          <h1 className="page-title">資料からAnkiカードを作成します。</h1>
          <p className="page-lead">ファイル、テキスト、テーマ入力から既存の汎用フローでカードを作成できます。</p>
        </section>
      </div>
    );
  }

  return <PurposeSelection />;
}
```

- [ ] **Step 4: Add CSS classes**

Add to `app/globals.css`:

```css
.purpose-shell,
.guide-shell {
  max-width: 1120px;
  width: 100%;
  margin: 0 auto;
}

.purpose-head {
  max-width: 760px;
  margin-bottom: 28px;
}

.purpose-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.purpose-card,
.guide-step {
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--card);
  box-shadow: var(--shadow);
  padding: 22px;
}

.purpose-card h2,
.guide-step h2 {
  margin: 0 0 8px;
}

.purpose-card p {
  margin: 0;
  color: var(--ink-soft);
  line-height: 1.6;
}

.guide-stack {
  display: grid;
  gap: 18px;
}

.guide-step__label {
  display: inline-flex;
  margin-bottom: 8px;
  color: var(--accent-deep);
  font-weight: 800;
  font-size: 0.78rem;
  text-transform: uppercase;
}

.choice-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.choice-button {
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--card-strong);
  color: var(--ink);
  padding: 14px 12px;
  cursor: pointer;
}

.choice-button.is-active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

@media (max-width: 860px) {
  .purpose-grid,
  .choice-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .purpose-grid,
  .choice-grid {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 5: Run type check**

Run: `npm run check`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add components/purpose-selection.tsx components/junior-exam-create-form.tsx app/page.tsx app/globals.css
git commit -m "feat: add junior exam guided creation UI"
```

## Task 6: Add Quality Review Summary, Filtering, and Labels

**Files:**
- Modify: `components/job-review.tsx`
- Modify: `components/review-card-editor.tsx`
- Modify: `app/globals.css`

- [ ] **Step 1: Add quality flag labels**

In `components/review-card-editor.tsx`, add:

```ts
const QUALITY_FLAG_LABELS: Record<string, string> = {
  too_long: "長すぎる",
  multi_point: "複数ポイント",
  source_check: "出典確認",
  strategy_mismatch: "方針確認",
  solution_gap: "解法不足",
  low_confidence: "要確認",
};
```

Render inside `.review-meta`:

```tsx
{card.qualityFlags.map((flag) => (
  <span key={flag} className="badge quality-badge">
    {QUALITY_FLAG_LABELS[flag] ?? "要確認"}
  </span>
))}
```

- [ ] **Step 2: Add review filter state**

In `components/job-review.tsx`, add:

```ts
const [showOnlyFlagged, setShowOnlyFlagged] = useState(false);
const flaggedCards = cards.filter((card) => card.qualityFlags.length > 0);
const visibleCards = showOnlyFlagged ? flaggedCards : cards;
```

Render above the card list:

```tsx
<div className="quality-summary">
  <div>
    <strong>要確認 {flaggedCards.length}</strong>
    <span className="muted-text"> / 全部 {cards.length}</span>
  </div>
  <button
    className={showOnlyFlagged ? "button-secondary is-active" : "button-secondary"}
    type="button"
    onClick={() => setShowOnlyFlagged((value) => !value)}
    disabled={flaggedCards.length === 0}
  >
    {showOnlyFlagged ? "すべて表示" : "要確認だけ表示"}
  </button>
</div>
```

Map `visibleCards` instead of `cards`.

- [ ] **Step 3: Add quality styles**

In `app/globals.css`, add:

```css
.quality-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--card);
  padding: 14px 16px;
  margin-bottom: 16px;
}

.quality-badge {
  border-color: rgba(156, 53, 42, 0.24);
  background: rgba(156, 53, 42, 0.1);
  color: var(--danger);
}

.button-secondary.is-active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
```

- [ ] **Step 4: Run type check**

Run: `npm run check`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add components/job-review.tsx components/review-card-editor.tsx app/globals.css
git commit -m "feat: show quality review flags"
```

## Task 7: Ensure Quality Flags Do Not Export as Anki Tags

**Files:**
- Modify: `__tests__/anki-export.test.ts`
- Modify: `__tests__/api-job-export.test.ts`

- [ ] **Step 1: Update export tests**

In `__tests__/anki-export.test.ts`, update `makeCard` to include:

```ts
qualityFlags: [],
```

Add:

```ts
it("does not export app-only quality flags as tags", () => {
  const cards = [
    makeCard({
      tags: ["sansuu"],
      qualityFlags: ["source_check", "too_long"],
    }),
  ];

  const result = buildTsv(cards).toString("utf8");
  const cardLine = result.split("\n")[3];
  expect(cardLine.split("\t")[2]).toBe("sansuu");
  expect(cardLine).not.toContain("source_check");
  expect(cardLine).not.toContain("too_long");
});
```

- [ ] **Step 2: Run export tests**

Run: `npm test -- __tests__/anki-export.test.ts __tests__/api-job-export.test.ts`

Expected: PASS because exports already use `tags`, not `qualityFlags`.

- [ ] **Step 3: Commit**

```bash
git add __tests__/anki-export.test.ts __tests__/api-job-export.test.ts
git commit -m "test: cover app-only quality flags in exports"
```

## Task 8: Full Verification and Browser Smoke Test

**Files:**
- No planned source edits unless verification exposes an issue.

- [ ] **Step 1: Run the full automated suite**

Run: `npm test`

Expected: PASS.

- [ ] **Step 2: Run type check**

Run: `npm run check`

Expected: PASS.

- [ ] **Step 3: Build the app**

Run: `npm run build`

Expected: PASS.

- [ ] **Step 4: Start the dev server**

Run: `npm run dev`

Expected: server starts and prints a local URL, usually `http://localhost:3000`.

- [ ] **Step 5: Browser smoke test**

Open the local URL in Browser and verify:

- Home page shows four purposes.
- `中学受験` opens the guided flow.
- Selecting 算数 + 小5 shows the 算数 summary.
- Submitting prompt input sends a job and routes to `/decks/<jobId>`.
- `自由作成` opens the generic form.
- The review page can display quality labels if cards have `qualityFlags`.

- [ ] **Step 6: Stop the dev server**

Stop the `npm run dev` session cleanly after browser verification.

- [ ] **Step 7: Commit any verification fixes**

If verification required source changes, commit them with:

```bash
git add <changed-files>
git commit -m "fix: polish junior exam guide verification issues"
```

If no source changes were needed, do not create an empty commit.

## Self-Review

- Spec coverage: The plan covers purpose entry, 中学受験 3-step flow, four subject strategies, Basic-only jobs, internal strategy prompts, quality flags, review UI, export exclusion, and preservation of generic flow.
- Scope check: The plan does not implement full 国語添削, structured wrong-answer entry, external fact checking, or user-configurable MCP/Skill flows.
- Type consistency: The plan uses `studyPurpose`, `examSubject`, `examStage`, `sourceMode`, and `qualityFlags` consistently across types, DB, API, LLM, and UI.
