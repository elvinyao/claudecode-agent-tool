# LLM Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a developer-facing LLM quality evaluation harness for guided Anki card generation.

**Architecture:** The harness lives under `eval/` with focused modules for fixtures, rule checks, LLM judging, reporting, and orchestration. The CLI entrypoint in `scripts/eval-llm.ts` uses `vite-node` so it can run TypeScript with the existing `@/*` path alias, while normal `npm test` remains fully mocked and does not call real LLMs.

**Tech Stack:** TypeScript, Vitest, Zod v3, existing OpenAI/Gemini clients, existing card generation provider interfaces, Node filesystem APIs.

---

## File Structure

- Create `eval/lib/types.ts`: shared eval fixture, result, score, and report types.
- Create `eval/fixtures/*.json`: seven fixed evaluation fixtures.
- Create `eval/lib/fixtures.ts`: fixture loading and validation.
- Create `eval/lib/rule-checker.ts`: deterministic checks for generated cards.
- Create `eval/lib/judge.ts`: judge prompt, judge response schema, provider selection, and response parsing.
- Create `eval/lib/reporter.ts`: terminal summary, JSON report, and Markdown report generation.
- Create `eval/lib/runner.ts`: fixture orchestration, generation provider injection, rule checks, judge calls, thresholds, and report assembly.
- Create `scripts/eval-llm.ts`: command-line entrypoint.
- Modify `package.json`: add `eval:llm` and `eval:llm:ci` scripts using `vite-node`.
- Modify `.gitignore`: ignore `eval/reports/`.
- Add tests in `__tests__/eval-fixtures.test.ts`, `__tests__/eval-rule-checker.test.ts`, `__tests__/eval-judge.test.ts`, `__tests__/eval-reporter.test.ts`, and `__tests__/eval-runner.test.ts`.

---

### Task 1: Eval Types, Fixtures, And Script Wiring

**Files:**
- Create: `eval/lib/types.ts`
- Create: `eval/lib/fixtures.ts`
- Create: `eval/fixtures/language-jlpt-vocabulary.json`
- Create: `eval/fixtures/language-grammar.json`
- Create: `eval/fixtures/language-translation-bidirectional.json`
- Create: `eval/fixtures/language-toeic-reading.json`
- Create: `eval/fixtures/junior-sansuu-mistake.json`
- Create: `eval/fixtures/junior-kokugo-vocabulary.json`
- Create: `eval/fixtures/junior-shakai-causality.json`
- Create: `__tests__/eval-fixtures.test.ts`
- Modify: `package.json`
- Modify: `.gitignore`

- [ ] **Step 1: Write fixture loading tests**

Create `__tests__/eval-fixtures.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { EVAL_FIXTURE_IDS, loadEvalFixtures } from "@/eval/lib/fixtures";

describe("loadEvalFixtures", () => {
  it("loads the seven first-version fixtures in stable order", () => {
    const fixtures = loadEvalFixtures();

    expect(fixtures.map((fixture) => fixture.id)).toEqual(EVAL_FIXTURE_IDS);
  });

  it("loads fixtures with source text, count ranges, and expected tags", () => {
    const fixtures = loadEvalFixtures();

    for (const fixture of fixtures) {
      expect(fixture.title.length).toBeGreaterThan(0);
      expect(fixture.source.text.length).toBeGreaterThan(50);
      expect(fixture.expectedCardCount.min).toBeGreaterThan(0);
      expect(fixture.expectedCardCount.max).toBeGreaterThanOrEqual(
        fixture.expectedCardCount.min,
      );
      expect(fixture.expectedTags.length).toBeGreaterThan(0);
      expect(fixture.rubricNotes.length).toBeGreaterThan(0);
    }
  });

  it("keeps fixtures on Basic cards only", () => {
    const fixtures = loadEvalFixtures();

    expect(fixtures.every((fixture) => fixture.job.cardType === "basic")).toBe(true);
  });
});
```

- [ ] **Step 2: Run the fixture tests and verify they fail**

Run:

```bash
npm test -- __tests__/eval-fixtures.test.ts
```

Expected: FAIL because `@/eval/lib/fixtures` does not exist yet.

- [ ] **Step 3: Add shared eval types**

Create `eval/lib/types.ts`:

```ts
import type {
  ContentChunk,
  GeneratedCard,
  JobRecord,
  LlmProvider,
} from "@/lib/types";

export type EvalMode = "local" | "strict";

export type EvalFixtureId =
  | "language-jlpt-vocabulary"
  | "language-grammar"
  | "language-translation-bidirectional"
  | "language-toeic-reading"
  | "junior-sansuu-mistake"
  | "junior-kokugo-vocabulary"
  | "junior-shakai-causality";

export type EvalDomainCheck =
  | "basic_schema"
  | "count_range"
  | "length"
  | "front_back_distinct"
  | "duplicate_cards"
  | "source_ref"
  | "expected_tags"
  | "source_grounding"
  | "junior_math_strategy"
  | "social_causality"
  | "translation_direction";

export interface EvalFixture {
  id: EvalFixtureId;
  title: string;
  job: Pick<
    JobRecord,
    | "deckName"
    | "inputType"
    | "cardType"
    | "requestedCardCount"
    | "language"
    | "studyPurpose"
    | "guideProfile"
    | "guideSelections"
    | "strategyVersion"
    | "examSubject"
    | "examStage"
    | "sourceMode"
    | "aiInstructions"
    | "userTags"
  >;
  source: {
    name: string;
    ref: string;
    text: string;
  };
  expectedCardCount: {
    min: number;
    max: number;
  };
  expectedTags: string[];
  maxFrontLength: number;
  maxBackLength: number;
  domainChecks: EvalDomainCheck[];
  rubricNotes: string[];
}

export interface EvalRuleIssue {
  check: EvalDomainCheck;
  severity: "warning" | "failure";
  message: string;
  cardIndex?: number;
}

export interface EvalRuleResult {
  passed: boolean;
  issues: EvalRuleIssue[];
}

export interface EvalJudgeScores {
  atomicity: number;
  grounding: number;
  learnerFit: number;
  usefulness: number;
  domainFit: number;
}

export interface EvalJudgeResult {
  provider: LlmProvider;
  model: string;
  scores: EvalJudgeScores;
  overallScore: number;
  reasons: string[];
  suggestions: string[];
}

export interface EvalCaseResult {
  fixture: EvalFixture;
  cards: GeneratedCard[];
  ruleResult: EvalRuleResult;
  judgeResult: EvalJudgeResult | null;
  status: "pass" | "warn" | "fail" | "skipped";
  totalScore: number | null;
  errorMessage: string | null;
}

export interface EvalReport {
  runId: string;
  mode: EvalMode;
  createdAt: string;
  generationProvider: LlmProvider | "unknown";
  judgeProvider: LlmProvider | "unknown";
  judgeModel: string | null;
  overallAverage: number | null;
  results: EvalCaseResult[];
}

export interface EvalGenerationProvider {
  name: LlmProvider;
  generateCardsForChunk(params: {
    job: JobRecord;
    chunk: ContentChunk;
    targetCardCount: number;
  }): Promise<GeneratedCard[]>;
}
```

- [ ] **Step 4: Add the fixture loader**

Create `eval/lib/fixtures.ts`:

```ts
import fs from "node:fs";
import path from "node:path";

import { z } from "zod/v3";

import type { EvalFixture, EvalFixtureId } from "@/eval/lib/types";

export const EVAL_FIXTURE_IDS: EvalFixtureId[] = [
  "language-jlpt-vocabulary",
  "language-grammar",
  "language-translation-bidirectional",
  "language-toeic-reading",
  "junior-sansuu-mistake",
  "junior-kokugo-vocabulary",
  "junior-shakai-causality",
];

const FixtureSchema = z.object({
  id: z.enum(EVAL_FIXTURE_IDS as [EvalFixtureId, ...EvalFixtureId[]]),
  title: z.string().min(1),
  job: z.object({
    deckName: z.string().min(1),
    inputType: z.enum(["upload", "prompt", "pasted_text"]),
    cardType: z.literal("basic"),
    requestedCardCount: z.number().int().positive().nullable(),
    language: z.string(),
    studyPurpose: z.enum(["junior_exam", "language", "exam_prep", "free"]),
    guideProfile: z.enum([
      "junior_exam.standard4",
      "language.general",
      "exam_prep.generic",
      "free.generic",
    ]),
    guideSelections: z.record(z.string()),
    strategyVersion: z.enum([
      "junior-exam-v1",
      "language-v1",
      "exam-prep-v1",
      "free-v1",
    ]),
    examSubject: z.enum(["kokugo", "sansuu", "rika", "shakai"]).nullable(),
    examStage: z.enum(["grade4", "grade5", "grade6", "final_push"]).nullable(),
    sourceMode: z.enum(["materials_notes", "mistakes_explanations", "topic_scope"]),
    aiInstructions: z.string(),
    userTags: z.array(z.string()),
  }),
  source: z.object({
    name: z.string().min(1),
    ref: z.string().min(1),
    text: z.string().min(50),
  }),
  expectedCardCount: z.object({
    min: z.number().int().positive(),
    max: z.number().int().positive(),
  }),
  expectedTags: z.array(z.string().min(1)).min(1),
  maxFrontLength: z.number().int().positive(),
  maxBackLength: z.number().int().positive(),
  domainChecks: z.array(
    z.enum([
      "basic_schema",
      "count_range",
      "length",
      "front_back_distinct",
      "duplicate_cards",
      "source_ref",
      "expected_tags",
      "source_grounding",
      "junior_math_strategy",
      "social_causality",
      "translation_direction",
    ]),
  ),
  rubricNotes: z.array(z.string().min(1)).min(1),
});

const fixturesDir = path.join(process.cwd(), "eval", "fixtures");

export function loadEvalFixtures(): EvalFixture[] {
  return EVAL_FIXTURE_IDS.map((id) => {
    const filePath = path.join(fixturesDir, `${id}.json`);
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw) as unknown;
    const fixture = FixtureSchema.parse(parsed);

    if (fixture.expectedCardCount.max < fixture.expectedCardCount.min) {
      throw new Error(`Invalid expectedCardCount range in ${id}.`);
    }

    return fixture;
  });
}
```

- [ ] **Step 5: Add fixture JSON files**

Create the seven JSON files named in `EVAL_FIXTURE_IDS`. Use the following exact content for the first file, then create the remaining six with the same schema and the fixture-specific values shown after it.

`eval/fixtures/language-jlpt-vocabulary.json`:

```json
{
  "id": "language-jlpt-vocabulary",
  "title": "JLPT N4 vocabulary cards",
  "job": {
    "deckName": "JLPT N4 語彙",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 5,
    "language": "ja+zh",
    "studyPurpose": "language",
    "guideProfile": "language.general",
    "guideSelections": {
      "learningLanguage": "ja",
      "learningLanguageCustom": "",
      "nativeLanguage": "zh",
      "nativeLanguageCustom": "",
      "targetExam": "jlpt_n4",
      "targetExamCustom": "",
      "taskType": "vocabulary",
      "translationDirection": "learning_to_native"
    },
    "strategyVersion": "language-v1",
    "examSubject": null,
    "examStage": null,
    "sourceMode": "materials_notes",
    "aiInstructions": "短く復習しやすいBasicカードにする。",
    "userTags": ["eval", "language", "jlpt-n4"]
  },
  "source": {
    "name": "jlpt-vocab-note",
    "ref": "jlpt-vocab-note · sample",
    "text": "語彙メモ：相談する＝困ったことや決めたいことについて人に意見を聞く。例文：進路について先生に相談する。準備する＝必要なものや予定を前もって整える。例文：旅行の準備をする。比べる＝二つ以上のものを見て違いや同じ点を考える。例文：二つの答えを比べる。"
  },
  "expectedCardCount": { "min": 3, "max": 6 },
  "expectedTags": ["language", "jlpt-n4", "vocabulary"],
  "maxFrontLength": 120,
  "maxBackLength": 220,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding"],
  "rubricNotes": ["Meanings should be concise in Chinese or simple Japanese.", "Cards should separate meaning, usage, and example points instead of merging all vocabulary into one card."]
}
```

Use these details for the remaining fixture files:

```json
{
  "id": "language-grammar",
  "title": "Japanese grammar pattern cards",
  "job": {
    "deckName": "日本語文法",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 4,
    "language": "ja+zh",
    "studyPurpose": "language",
    "guideProfile": "language.general",
    "guideSelections": {
      "learningLanguage": "ja",
      "learningLanguageCustom": "",
      "nativeLanguage": "zh",
      "nativeLanguageCustom": "",
      "targetExam": "jlpt_n3",
      "targetExamCustom": "",
      "taskType": "grammar",
      "translationDirection": "learning_to_native"
    },
    "strategyVersion": "language-v1",
    "examSubject": null,
    "examStage": null,
    "sourceMode": "materials_notes",
    "aiInstructions": "接続、意味、誤用を分ける。",
    "userTags": ["eval", "language", "grammar"]
  },
  "source": {
    "name": "grammar-note",
    "ref": "grammar-note · sample",
    "text": "文法メモ：〜てしまう。意味は、完了、または残念な気持ちを表す。接続は動詞のて形＋しまう。例文：宿題を忘れてしまった。注意：ただの過去形ではなく、話し手の気持ちが入ることが多い。"
  },
  "expectedCardCount": { "min": 3, "max": 5 },
  "expectedTags": ["language", "grammar", "jlpt-n3"],
  "maxFrontLength": 130,
  "maxBackLength": 240,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding"],
  "rubricNotes": ["Separate connection, meaning, example, and common mistake.", "Do not use cloze syntax."]
}
```

```json
{
  "id": "language-translation-bidirectional",
  "title": "Bidirectional translation practice",
  "job": {
    "deckName": "翻訳練習",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 4,
    "language": "en+zh",
    "studyPurpose": "language",
    "guideProfile": "language.general",
    "guideSelections": {
      "learningLanguage": "en",
      "learningLanguageCustom": "",
      "nativeLanguage": "zh",
      "nativeLanguageCustom": "",
      "targetExam": "none",
      "targetExamCustom": "",
      "taskType": "translation_practice",
      "translationDirection": "bidirectional"
    },
    "strategyVersion": "language-v1",
    "examSubject": null,
    "examStage": null,
    "sourceMode": "materials_notes",
    "aiInstructions": "英語から中国語、中国語から英語の両方向を含める。",
    "userTags": ["eval", "language", "translation"]
  },
  "source": {
    "name": "translation-note",
    "ref": "translation-note · sample",
    "text": "Translation practice phrases: Could you explain this part again? / I am not sure which answer is correct. / 这个句子是什么意思？ / 我想用英语更自然地表达这个想法。Keep each practice item short and useful for classroom or self-study situations."
  },
  "expectedCardCount": { "min": 4, "max": 6 },
  "expectedTags": ["language", "translation-practice", "bidirectional"],
  "maxFrontLength": 150,
  "maxBackLength": 220,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding", "translation_direction"],
  "rubricNotes": ["There should be evidence of both English to Chinese and Chinese to English practice.", "Each card should ask for one short phrase or sentence."]
}
```

```json
{
  "id": "language-toeic-reading",
  "title": "TOEIC reading material cards",
  "job": {
    "deckName": "TOEIC Reading",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 5,
    "language": "en+zh",
    "studyPurpose": "language",
    "guideProfile": "language.general",
    "guideSelections": {
      "learningLanguage": "en",
      "learningLanguageCustom": "",
      "nativeLanguage": "zh",
      "nativeLanguageCustom": "",
      "targetExam": "toeic",
      "targetExamCustom": "",
      "taskType": "listening_reading",
      "translationDirection": "learning_to_native"
    },
    "strategyVersion": "language-v1",
    "examSubject": null,
    "examStage": null,
    "sourceMode": "materials_notes",
    "aiInstructions": "主旨、詳細、言い換え表現をカード化する。",
    "userTags": ["eval", "language", "toeic"]
  },
  "source": {
    "name": "toeic-reading-note",
    "ref": "toeic-reading-note · sample",
    "text": "Memo: The sales department will move to the fifth floor on Monday. Employees should pack personal items by Friday afternoon. The IT team will disconnect desktop computers at 6 p.m. Friday and reconnect them before Monday morning. Key phrase: by Friday afternoon means no later than Friday afternoon."
  },
  "expectedCardCount": { "min": 4, "max": 6 },
  "expectedTags": ["language", "toeic", "listening-reading"],
  "maxFrontLength": 160,
  "maxBackLength": 240,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding"],
  "rubricNotes": ["Cards should include main idea, detail, and paraphrase-style TOEIC reading points.", "Avoid overly broad summary cards."]
}
```

```json
{
  "id": "junior-sansuu-mistake",
  "title": "中学受験算数 mistake review",
  "job": {
    "deckName": "小5算数つまずき",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 5,
    "language": "ja",
    "studyPurpose": "junior_exam",
    "guideProfile": "junior_exam.standard4",
    "guideSelections": {},
    "strategyVersion": "junior-exam-v1",
    "examSubject": "sansuu",
    "examStage": "grade5",
    "sourceMode": "mistakes_explanations",
    "aiInstructions": "記憶カードを中心に、少量の解法の入口カードを含める。",
    "userTags": ["eval", "chugaku-juken", "sansuu"]
  },
  "source": {
    "name": "sansuu-mistake-note",
    "ref": "sansuu-mistake-note · sample",
    "text": "問題：ある本を全体の3/5読みました。残りは何分のいくつですか。子どもの答え：3/5。正解：2/5。つまずき：問題文の「残り」を見落とした。考え方：全体を1=5/5と見て、5/5-3/5=2/5。次からは、何を聞かれているかに線を引く。"
  },
  "expectedCardCount": { "min": 4, "max": 6 },
  "expectedTags": ["chugaku-juken", "sansuu", "mistake-review"],
  "maxFrontLength": 120,
  "maxBackLength": 240,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding", "junior_math_strategy"],
  "rubricNotes": ["Cards should capture the condition, why 1 becomes 5/5, the common mistake, and the next action.", "Do not write a long full solution in one card."]
}
```

```json
{
  "id": "junior-kokugo-vocabulary",
  "title": "中学受験国語 vocabulary cards",
  "job": {
    "deckName": "小5国語語句",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 4,
    "language": "ja",
    "studyPurpose": "junior_exam",
    "guideProfile": "junior_exam.standard4",
    "guideSelections": {},
    "strategyVersion": "junior-exam-v1",
    "examSubject": "kokugo",
    "examStage": "grade5",
    "sourceMode": "materials_notes",
    "aiInstructions": "一言まとめと使い方を短く分ける。",
    "userTags": ["eval", "chugaku-juken", "kokugo"]
  },
  "source": {
    "name": "kokugo-vocab-note",
    "ref": "kokugo-vocab-note · sample",
    "text": "語句メモ：ためらう＝すぐに決めたり行動したりできず迷うこと。例：発表する前に少しためらう。見当をつける＝だいたいの予想をすること。例：文章全体を読んで答えの見当をつける。国語では意味だけでなく、文中でどう使われるかを覚える。"
  },
  "expectedCardCount": { "min": 3, "max": 5 },
  "expectedTags": ["chugaku-juken", "kokugo", "materials"],
  "maxFrontLength": 120,
  "maxBackLength": 220,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding"],
  "rubricNotes": ["Cards should be short memory cards for meaning and usage.", "Avoid literary analysis that is not present in the source."]
}
```

```json
{
  "id": "junior-shakai-causality",
  "title": "中学受験社会 causality cards",
  "job": {
    "deckName": "小6社会因果",
    "inputType": "pasted_text",
    "cardType": "basic",
    "requestedCardCount": 5,
    "language": "ja",
    "studyPurpose": "junior_exam",
    "guideProfile": "junior_exam.standard4",
    "guideSelections": {},
    "strategyVersion": "junior-exam-v1",
    "examSubject": "shakai",
    "examStage": "grade6",
    "sourceMode": "materials_notes",
    "aiInstructions": "人物・事件・因果を短いカードにする。",
    "userTags": ["eval", "chugaku-juken", "shakai"]
  },
  "source": {
    "name": "shakai-causality-note",
    "ref": "shakai-causality-note · sample",
    "text": "歴史メモ：鎌倉幕府では御恩と奉公の関係が重要。御恩とは将軍が御家人に領地を認めたり新しい領地を与えたりすること。奉公とは御家人が戦いのときに将軍のために働くこと。この関係により、将軍と御家人の結びつきが強まった。"
  },
  "expectedCardCount": { "min": 4, "max": 6 },
  "expectedTags": ["chugaku-juken", "shakai", "materials"],
  "maxFrontLength": 130,
  "maxBackLength": 240,
  "domainChecks": ["basic_schema", "count_range", "length", "front_back_distinct", "duplicate_cards", "source_ref", "expected_tags", "source_grounding", "social_causality"],
  "rubricNotes": ["Cards should preserve the relationship between cause and result.", "The terms 御恩 and 奉公 should not be merged into a vague single summary."]
}
```

- [ ] **Step 6: Add package scripts and gitignore entry**

Modify `package.json` scripts:

```json
{
  "dev": "next dev",
  "build": "next build",
  "start": "next start",
  "start:standalone": "NODE_OPTIONS=--no-warnings node .next/standalone/server.js",
  "check": "tsc --noEmit",
  "test": "vitest run",
  "test:watch": "vitest",
  "eval:llm": "vite-node scripts/eval-llm.ts",
  "eval:llm:ci": "vite-node scripts/eval-llm.ts --strict"
}
```

Append this line to `.gitignore`:

```gitignore
eval/reports/
```

- [ ] **Step 7: Run fixture tests and typecheck**

Run:

```bash
npm test -- __tests__/eval-fixtures.test.ts
npm run check
```

Expected: fixture tests PASS and typecheck PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add package.json .gitignore eval/fixtures eval/lib/types.ts eval/lib/fixtures.ts __tests__/eval-fixtures.test.ts
git commit -m "feat: add llm eval fixtures"
```

---

### Task 2: Deterministic Rule Checker

**Files:**
- Create: `eval/lib/rule-checker.ts`
- Create: `__tests__/eval-rule-checker.test.ts`

- [ ] **Step 1: Write rule checker tests**

Create `__tests__/eval-rule-checker.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { checkGeneratedCards } from "@/eval/lib/rule-checker";
import type { EvalFixture } from "@/eval/lib/types";
import type { GeneratedCard } from "@/lib/types";

function makeFixture(overrides: Partial<EvalFixture> = {}): EvalFixture {
  return {
    id: "junior-sansuu-mistake",
    title: "Fixture",
    job: {
      deckName: "Deck",
      inputType: "pasted_text",
      cardType: "basic",
      requestedCardCount: 2,
      language: "ja",
      studyPurpose: "junior_exam",
      guideProfile: "junior_exam.standard4",
      guideSelections: {},
      strategyVersion: "junior-exam-v1",
      examSubject: "sansuu",
      examStage: "grade5",
      sourceMode: "mistakes_explanations",
      aiInstructions: "",
      userTags: ["eval", "sansuu"],
    },
    source: {
      name: "source",
      ref: "source · sample",
      text: "全体を1=5/5と見て、5/5-3/5=2/5。問題文の残りを見落とした。",
    },
    expectedCardCount: { min: 2, max: 3 },
    expectedTags: ["eval", "sansuu"],
    maxFrontLength: 80,
    maxBackLength: 120,
    domainChecks: [
      "basic_schema",
      "count_range",
      "length",
      "front_back_distinct",
      "duplicate_cards",
      "source_ref",
      "expected_tags",
      "source_grounding",
      "junior_math_strategy",
    ],
    rubricNotes: ["Use math mistake cards."],
    ...overrides,
  };
}

function makeCard(overrides: Partial<GeneratedCard> = {}): GeneratedCard {
  return {
    front: "残りを求めるとき、全体はどう表す？",
    back: "全体を1=5/5と表す。",
    tags: ["eval", "sansuu"],
    qualityFlags: [],
    sourceRef: "source · sample",
    confidence: 0.9,
    ...overrides,
  };
}

describe("checkGeneratedCards", () => {
  it("passes focused cards that satisfy fixture rules", () => {
    const result = checkGeneratedCards(makeFixture(), [
      makeCard(),
      makeCard({
        front: "この問題のつまずきは？",
        back: "「残り」を見落として3/5を答えにしたこと。",
      }),
    ]);

    expect(result.passed).toBe(true);
    expect(result.issues).toEqual([]);
  });

  it("fails when card count is outside the expected range", () => {
    const result = checkGeneratedCards(makeFixture(), [makeCard()]);

    expect(result.passed).toBe(false);
    expect(result.issues).toContainEqual(
      expect.objectContaining({ check: "count_range", severity: "failure" }),
    );
  });

  it("flags long fronts and backs", () => {
    const result = checkGeneratedCards(makeFixture(), [
      makeCard({ front: "x".repeat(81) }),
      makeCard({ back: "y".repeat(121) }),
    ]);

    expect(result.issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ check: "length", cardIndex: 0 }),
        expect.objectContaining({ check: "length", cardIndex: 1 }),
      ]),
    );
  });

  it("flags duplicate fronts", () => {
    const result = checkGeneratedCards(makeFixture(), [makeCard(), makeCard()]);

    expect(result.issues).toContainEqual(
      expect.objectContaining({ check: "duplicate_cards", severity: "failure" }),
    );
  });

  it("flags missing expected tags", () => {
    const result = checkGeneratedCards(makeFixture(), [
      makeCard({ tags: ["eval"] }),
      makeCard({ front: "別の問い", tags: ["eval"] }),
    ]);

    expect(result.issues).toContainEqual(
      expect.objectContaining({ check: "expected_tags", severity: "failure" }),
    );
  });

  it("flags unsupported content as a source grounding warning", () => {
    const result = checkGeneratedCards(makeFixture(), [
      makeCard({ front: "徳川家康は何をした？", back: "江戸幕府を開いた。" }),
      makeCard({ front: "この問題のつまずきは？", back: "残りを見落とした。" }),
    ]);

    expect(result.issues).toContainEqual(
      expect.objectContaining({ check: "source_grounding", severity: "warning" }),
    );
  });
});
```

- [ ] **Step 2: Run the rule checker tests and verify they fail**

Run:

```bash
npm test -- __tests__/eval-rule-checker.test.ts
```

Expected: FAIL because `@/eval/lib/rule-checker` does not exist yet.

- [ ] **Step 3: Implement `checkGeneratedCards`**

Create `eval/lib/rule-checker.ts`:

```ts
import type {
  EvalDomainCheck,
  EvalFixture,
  EvalRuleIssue,
  EvalRuleResult,
} from "@/eval/lib/types";
import type { GeneratedCard } from "@/lib/types";

function normalize(value: string) {
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}

function words(value: string) {
  return normalize(value)
    .split(/[^\p{L}\p{N}]+/u)
    .filter((token) => token.length >= 2);
}

function addIssue(
  issues: EvalRuleIssue[],
  check: EvalDomainCheck,
  severity: "warning" | "failure",
  message: string,
  cardIndex?: number,
) {
  issues.push({ check, severity, message, cardIndex });
}

function includesCheck(fixture: EvalFixture, check: EvalDomainCheck) {
  return fixture.domainChecks.includes(check);
}

function hasSourceOverlap(card: GeneratedCard, sourceText: string) {
  const sourceTokens = new Set(words(sourceText));
  const cardTokens = words(`${card.front} ${card.back}`);
  const overlapping = cardTokens.filter((token) => sourceTokens.has(token));

  return overlapping.length >= Math.min(2, cardTokens.length);
}

function hasAnyTerm(cards: GeneratedCard[], terms: string[]) {
  const combined = normalize(cards.map((card) => `${card.front} ${card.back}`).join(" "));
  return terms.some((term) => combined.includes(normalize(term)));
}

export function checkGeneratedCards(
  fixture: EvalFixture,
  cards: GeneratedCard[],
): EvalRuleResult {
  const issues: EvalRuleIssue[] = [];

  if (includesCheck(fixture, "basic_schema")) {
    cards.forEach((card, index) => {
      if (!card.front.trim() || !card.back.trim()) {
        addIssue(issues, "basic_schema", "failure", "Front and back must be non-empty.", index);
      }

      if (card.front.includes("{{c") || card.back.includes("{{c")) {
        addIssue(issues, "basic_schema", "failure", "Eval v1 expects Basic cards only.", index);
      }
    });
  }

  if (
    includesCheck(fixture, "count_range") &&
    (cards.length < fixture.expectedCardCount.min || cards.length > fixture.expectedCardCount.max)
  ) {
    addIssue(
      issues,
      "count_range",
      "failure",
      `Expected ${fixture.expectedCardCount.min}-${fixture.expectedCardCount.max} cards, got ${cards.length}.`,
    );
  }

  if (includesCheck(fixture, "length")) {
    cards.forEach((card, index) => {
      if (card.front.length > fixture.maxFrontLength) {
        addIssue(
          issues,
          "length",
          "warning",
          `Front is ${card.front.length} chars, limit is ${fixture.maxFrontLength}.`,
          index,
        );
      }

      if (card.back.length > fixture.maxBackLength) {
        addIssue(
          issues,
          "length",
          "warning",
          `Back is ${card.back.length} chars, limit is ${fixture.maxBackLength}.`,
          index,
        );
      }
    });
  }

  if (includesCheck(fixture, "front_back_distinct")) {
    cards.forEach((card, index) => {
      if (normalize(card.front) === normalize(card.back)) {
        addIssue(
          issues,
          "front_back_distinct",
          "failure",
          "Front and back should not be identical.",
          index,
        );
      }
    });
  }

  if (includesCheck(fixture, "duplicate_cards")) {
    const seen = new Map<string, number>();
    cards.forEach((card, index) => {
      const key = normalize(card.front);
      const firstIndex = seen.get(key);

      if (firstIndex !== undefined) {
        addIssue(
          issues,
          "duplicate_cards",
          "failure",
          `Duplicate front matches card ${firstIndex + 1}.`,
          index,
        );
      } else {
        seen.set(key, index);
      }
    });
  }

  if (includesCheck(fixture, "source_ref")) {
    cards.forEach((card, index) => {
      if (!card.sourceRef?.trim()) {
        addIssue(issues, "source_ref", "failure", "source_ref is required.", index);
      }
    });
  }

  if (includesCheck(fixture, "expected_tags")) {
    const tags = new Set(cards.flatMap((card) => card.tags.map(normalize)));
    for (const expectedTag of fixture.expectedTags) {
      if (!tags.has(normalize(expectedTag))) {
        addIssue(
          issues,
          "expected_tags",
          "failure",
          `Expected tag "${expectedTag}" was not found.`,
        );
      }
    }
  }

  if (includesCheck(fixture, "source_grounding")) {
    cards.forEach((card, index) => {
      if (!hasSourceOverlap(card, fixture.source.text)) {
        addIssue(
          issues,
          "source_grounding",
          "warning",
          "Card has weak lexical overlap with the source material.",
          index,
        );
      }
    });
  }

  if (
    includesCheck(fixture, "junior_math_strategy") &&
    !hasAnyTerm(cards, ["式", "条件", "つまずき", "ミス", "なぜ", "残り", "全体"])
  ) {
    addIssue(
      issues,
      "junior_math_strategy",
      "warning",
      "Math review should mention a formula, condition, step, or mistake point.",
    );
  }

  if (
    includesCheck(fixture, "social_causality") &&
    !hasAnyTerm(cards, ["ため", "により", "関係", "原因", "結果", "結びつき"])
  ) {
    addIssue(
      issues,
      "social_causality",
      "warning",
      "Social studies cards should preserve cause/effect wording.",
    );
  }

  if (
    includesCheck(fixture, "translation_direction") &&
    !hasAnyTerm(cards, ["英語", "中国語", "日本語", "translate", "translation", "訳", "翻訳"])
  ) {
    addIssue(
      issues,
      "translation_direction",
      "warning",
      "Translation fixture should show the requested translation direction.",
    );
  }

  return {
    passed: !issues.some((issue) => issue.severity === "failure"),
    issues,
  };
}
```

- [ ] **Step 4: Run rule checker tests**

Run:

```bash
npm test -- __tests__/eval-rule-checker.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add eval/lib/rule-checker.ts __tests__/eval-rule-checker.test.ts
git commit -m "feat: add llm eval rule checks"
```

---

### Task 3: LLM Judge Provider And Parser

**Files:**
- Create: `eval/lib/judge.ts`
- Create: `__tests__/eval-judge.test.ts`

- [ ] **Step 1: Write judge parser tests**

Create `__tests__/eval-judge.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import {
  buildJudgePrompt,
  parseJudgeResponse,
  resolveJudgeConfig,
} from "@/eval/lib/judge";
import type { EvalFixture } from "@/eval/lib/types";
import type { GeneratedCard } from "@/lib/types";

const fixture = {
  id: "language-translation-bidirectional",
  title: "Translation",
  source: { name: "source", ref: "source · sample", text: "Could you explain this?" },
  rubricNotes: ["Respect bidirectional translation."],
} as EvalFixture;

const cards: GeneratedCard[] = [
  {
    front: "Translate into Chinese: Could you explain this?",
    back: "你能解释一下这个吗？",
    tags: ["language", "translation-practice"],
    qualityFlags: [],
    sourceRef: "source · sample",
    confidence: 0.9,
  },
];

describe("parseJudgeResponse", () => {
  it("parses structured judge JSON and computes the overall score", () => {
    const result = parseJudgeResponse(
      JSON.stringify({
        scores: {
          atomicity: 90,
          grounding: 85,
          learnerFit: 80,
          usefulness: 88,
          domainFit: 92
        },
        reasons: ["Cards are focused."],
        suggestions: ["Add one reverse direction card."]
      }),
      "openai",
      "gpt-4.1-mini",
    );

    expect(result.overallScore).toBe(87);
    expect(result.provider).toBe("openai");
    expect(result.model).toBe("gpt-4.1-mini");
  });

  it("throws on invalid judge JSON", () => {
    expect(() => parseJudgeResponse("not json", "openai", "model")).toThrow(
      "Judge returned invalid JSON",
    );
  });
});

describe("buildJudgePrompt", () => {
  it("includes fixture rubric notes, source, and cards", () => {
    const prompt = buildJudgePrompt({ fixture, cards });

    expect(prompt).toContain("Respect bidirectional translation.");
    expect(prompt).toContain("Could you explain this?");
    expect(prompt).toContain("translation-practice");
  });
});

describe("resolveJudgeConfig", () => {
  it("uses app provider when no judge override is supplied", () => {
    const config = resolveJudgeConfig({
      appProvider: "gemini",
      appModel: "gemini-2.5-flash",
      env: {},
    });

    expect(config).toEqual({
      provider: "gemini",
      model: "gemini-2.5-flash",
    });
  });

  it("uses judge override provider and model", () => {
    const config = resolveJudgeConfig({
      appProvider: "gemini",
      appModel: "gemini-2.5-flash",
      env: {
        EVAL_JUDGE_PROVIDER: "openai",
        EVAL_JUDGE_MODEL: "gpt-4.1-mini",
      },
    });

    expect(config).toEqual({
      provider: "openai",
      model: "gpt-4.1-mini",
    });
  });
});
```

- [ ] **Step 2: Run judge tests and verify they fail**

Run:

```bash
npm test -- __tests__/eval-judge.test.ts
```

Expected: FAIL because `@/eval/lib/judge` does not exist yet.

- [ ] **Step 3: Implement judge parser, prompt, and provider calls**

Create `eval/lib/judge.ts`:

```ts
import { GoogleGenAI } from "@google/genai";
import OpenAI from "openai";
import { z } from "zod/v3";

import type { EvalFixture, EvalJudgeResult } from "@/eval/lib/types";
import { getServerEnv } from "@/lib/env";
import type { GeneratedCard, LlmProvider } from "@/lib/types";

const JudgeSchema = z.object({
  scores: z.object({
    atomicity: z.number().min(0).max(100),
    grounding: z.number().min(0).max(100),
    learnerFit: z.number().min(0).max(100),
    usefulness: z.number().min(0).max(100),
    domainFit: z.number().min(0).max(100),
  }),
  reasons: z.array(z.string()).default([]),
  suggestions: z.array(z.string()).default([]),
});

export interface JudgeConfig {
  provider: LlmProvider;
  model: string;
}

function parseProvider(value: string): LlmProvider {
  const normalized = value.trim().toLowerCase();

  if (normalized === "openai" || normalized === "gemini") {
    return normalized;
  }

  throw new Error(`Unsupported EVAL_JUDGE_PROVIDER "${value}". Use "openai" or "gemini".`);
}

export function resolveJudgeConfig(params: {
  appProvider: LlmProvider;
  appModel: string;
  env?: Record<string, string | undefined>;
}): JudgeConfig {
  const env = params.env ?? process.env;
  const provider = env.EVAL_JUDGE_PROVIDER
    ? parseProvider(env.EVAL_JUDGE_PROVIDER)
    : params.appProvider;
  const model = env.EVAL_JUDGE_MODEL?.trim() || params.appModel;

  return { provider, model };
}

export function buildJudgePrompt(params: {
  fixture: Pick<EvalFixture, "id" | "title" | "source" | "rubricNotes">;
  cards: GeneratedCard[];
}) {
  return [
    "You are judging generated Anki Basic flashcards for study quality.",
    "Return JSON only with this shape:",
    '{"scores":{"atomicity":0,"grounding":0,"learnerFit":0,"usefulness":0,"domainFit":0},"reasons":[],"suggestions":[]}',
    "Scores are 0-100. Be strict about unsupported facts, multi-point cards, and weak recall prompts.",
    `Fixture: ${params.fixture.id} - ${params.fixture.title}`,
    "Rubric notes:",
    params.fixture.rubricNotes.map((note) => `- ${note}`).join("\n"),
    "Source material:",
    params.fixture.source.text,
    "Generated cards:",
    JSON.stringify(params.cards, null, 2),
  ].join("\n\n");
}

export function parseJudgeResponse(
  rawText: string,
  provider: LlmProvider,
  model: string,
): EvalJudgeResult {
  let parsedJson: unknown;

  try {
    parsedJson = JSON.parse(rawText);
  } catch (error) {
    throw new Error(
      `Judge returned invalid JSON: ${error instanceof Error ? error.message : "unknown parse error"}`,
    );
  }

  const parsed = JudgeSchema.parse(parsedJson);
  const scoreValues = Object.values(parsed.scores);
  const overallScore = Math.round(
    scoreValues.reduce((sum, score) => sum + score, 0) / scoreValues.length,
  );

  return {
    provider,
    model,
    scores: parsed.scores,
    overallScore,
    reasons: parsed.reasons,
    suggestions: parsed.suggestions,
  };
}

async function judgeWithOpenAi(params: {
  apiKey: string;
  model: string;
  prompt: string;
}): Promise<string> {
  const client = new OpenAI({ apiKey: params.apiKey });
  const response = await client.chat.completions.create({
    model: params.model,
    temperature: 0.1,
    response_format: { type: "json_object" },
    messages: [
      {
        role: "developer",
        content: "You are a strict flashcard quality evaluator. Return JSON only.",
      },
      { role: "user", content: params.prompt },
    ],
  });

  const text = response.choices[0]?.message?.content;

  if (!text) {
    throw new Error("OpenAI judge returned an empty response.");
  }

  return text;
}

async function judgeWithGemini(params: {
  apiKey: string;
  model: string;
  prompt: string;
}): Promise<string> {
  const client = new GoogleGenAI({ apiKey: params.apiKey });
  const response = await client.models.generateContent({
    model: params.model,
    contents: params.prompt,
    config: {
      temperature: 0.1,
      responseMimeType: "application/json",
    },
  });

  if (!response.text) {
    throw new Error("Gemini judge returned an empty response.");
  }

  return response.text;
}

export async function judgeGeneratedCards(params: {
  fixture: EvalFixture;
  cards: GeneratedCard[];
  config: JudgeConfig;
}): Promise<EvalJudgeResult> {
  const env = getServerEnv();
  const prompt = buildJudgePrompt(params);

  const rawText =
    params.config.provider === "openai"
      ? await judgeWithOpenAi({
          apiKey: env.openAiApiKey,
          model: params.config.model,
          prompt,
        })
      : await judgeWithGemini({
          apiKey: env.geminiApiKey,
          model: params.config.model,
          prompt,
        });

  return parseJudgeResponse(rawText, params.config.provider, params.config.model);
}
```

- [ ] **Step 4: Run judge tests**

Run:

```bash
npm test -- __tests__/eval-judge.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add eval/lib/judge.ts __tests__/eval-judge.test.ts
git commit -m "feat: add llm eval judge"
```

---

### Task 4: Report Generation

**Files:**
- Create: `eval/lib/reporter.ts`
- Create: `__tests__/eval-reporter.test.ts`

- [ ] **Step 1: Write reporter tests**

Create `__tests__/eval-reporter.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { buildMarkdownReport, buildTerminalSummary, serializeJsonReport } from "@/eval/lib/reporter";
import type { EvalReport } from "@/eval/lib/types";

const report = {
  runId: "eval-2026-05-26T00-00-00-000Z",
  mode: "local",
  createdAt: "2026-05-26T00:00:00.000Z",
  generationProvider: "openai",
  judgeProvider: "openai",
  judgeModel: "gpt-4.1-mini",
  overallAverage: 84,
  results: [
    {
      fixture: {
        id: "language-jlpt-vocabulary",
        title: "JLPT",
        source: { name: "source", ref: "source", text: "source text" },
      },
      cards: [
        {
          front: "相談する means?",
          back: "商量；咨询。",
          tags: ["language", "jlpt-n4"],
          qualityFlags: [],
          sourceRef: "source",
          confidence: 0.9,
        },
      ],
      ruleResult: { passed: true, issues: [] },
      judgeResult: {
        provider: "openai",
        model: "gpt-4.1-mini",
        scores: {
          atomicity: 90,
          grounding: 85,
          learnerFit: 80,
          usefulness: 85,
          domainFit: 80,
        },
        overallScore: 84,
        reasons: ["Focused cards."],
        suggestions: ["Add one usage card."],
      },
      status: "pass",
      totalScore: 84,
      errorMessage: null,
    },
  ],
} as EvalReport;

describe("eval reporter", () => {
  it("serializes stable JSON", () => {
    const json = serializeJsonReport(report);

    expect(JSON.parse(json)).toMatchObject({
      runId: "eval-2026-05-26T00-00-00-000Z",
      overallAverage: 84,
    });
  });

  it("builds a readable Markdown report", () => {
    const markdown = buildMarkdownReport(report);

    expect(markdown).toContain("# LLM Eval Report");
    expect(markdown).toContain("language-jlpt-vocabulary");
    expect(markdown).toContain("相談する means?");
    expect(markdown).toContain("Add one usage card.");
  });

  it("builds a compact terminal summary", () => {
    const summary = buildTerminalSummary(report);

    expect(summary).toContain("Overall average: 84");
    expect(summary).toContain("PASS language-jlpt-vocabulary");
  });
});
```

- [ ] **Step 2: Run reporter tests and verify they fail**

Run:

```bash
npm test -- __tests__/eval-reporter.test.ts
```

Expected: FAIL because `@/eval/lib/reporter` does not exist yet.

- [ ] **Step 3: Implement reporter helpers**

Create `eval/lib/reporter.ts`:

```ts
import fs from "node:fs/promises";
import path from "node:path";

import type { EvalCaseResult, EvalReport } from "@/eval/lib/types";

function statusLabel(status: EvalCaseResult["status"]) {
  return status.toUpperCase();
}

function formatScore(score: number | null) {
  return score === null ? "n/a" : String(score);
}

function sampleCards(result: EvalCaseResult) {
  return result.cards.slice(0, 3).map((card, index) => {
    return [
      `**Card ${index + 1}**`,
      `- Front: ${card.front}`,
      `- Back: ${card.back}`,
      `- Tags: ${card.tags.join(", ")}`,
      `- Source: ${card.sourceRef ?? "none"}`,
    ].join("\n");
  });
}

export function serializeJsonReport(report: EvalReport) {
  return `${JSON.stringify(report, null, 2)}\n`;
}

export function buildTerminalSummary(report: EvalReport) {
  const lines = [
    `LLM eval ${report.runId}`,
    `Mode: ${report.mode}`,
    `Generation provider: ${report.generationProvider}`,
    `Judge: ${report.judgeProvider}${report.judgeModel ? ` / ${report.judgeModel}` : ""}`,
    `Overall average: ${formatScore(report.overallAverage)}`,
    "",
  ];

  for (const result of report.results) {
    const issueCount = result.ruleResult.issues.length;
    lines.push(
      `${statusLabel(result.status)} ${result.fixture.id} score=${formatScore(result.totalScore)} cards=${result.cards.length} issues=${issueCount}`,
    );
  }

  return lines.join("\n");
}

export function buildMarkdownReport(report: EvalReport) {
  const sections = [
    "# LLM Eval Report",
    "",
    `- Run: ${report.runId}`,
    `- Created: ${report.createdAt}`,
    `- Mode: ${report.mode}`,
    `- Generation provider: ${report.generationProvider}`,
    `- Judge: ${report.judgeProvider}${report.judgeModel ? ` / ${report.judgeModel}` : ""}`,
    `- Overall average: ${formatScore(report.overallAverage)}`,
    "",
  ];

  for (const result of report.results) {
    sections.push(
      `## ${statusLabel(result.status)} ${result.fixture.id}`,
      "",
      `- Title: ${result.fixture.title}`,
      `- Score: ${formatScore(result.totalScore)}`,
      `- Cards: ${result.cards.length}`,
      `- Error: ${result.errorMessage ?? "none"}`,
      "",
      "### Rule Issues",
      "",
    );

    if (result.ruleResult.issues.length === 0) {
      sections.push("- none", "");
    } else {
      for (const issue of result.ruleResult.issues) {
        const cardLabel = issue.cardIndex === undefined ? "" : ` card=${issue.cardIndex + 1}`;
        sections.push(`- ${issue.severity} ${issue.check}${cardLabel}: ${issue.message}`);
      }
      sections.push("");
    }

    sections.push("### Judge", "");

    if (result.judgeResult) {
      sections.push(
        `- Overall: ${result.judgeResult.overallScore}`,
        `- Atomicity: ${result.judgeResult.scores.atomicity}`,
        `- Grounding: ${result.judgeResult.scores.grounding}`,
        `- Learner fit: ${result.judgeResult.scores.learnerFit}`,
        `- Usefulness: ${result.judgeResult.scores.usefulness}`,
        `- Domain fit: ${result.judgeResult.scores.domainFit}`,
        "",
        "Reasons:",
        ...result.judgeResult.reasons.map((reason) => `- ${reason}`),
        "",
        "Suggestions:",
        ...result.judgeResult.suggestions.map((suggestion) => `- ${suggestion}`),
        "",
      );
    } else {
      sections.push("- Judge result unavailable.", "");
    }

    sections.push("### Sample Cards", "", ...sampleCards(result), "");
  }

  return `${sections.join("\n")}\n`;
}

export async function writeEvalReports(report: EvalReport, reportsDir = path.join(process.cwd(), "eval", "reports")) {
  await fs.mkdir(reportsDir, { recursive: true });

  const jsonPath = path.join(reportsDir, `${report.runId}.json`);
  const markdownPath = path.join(reportsDir, `${report.runId}.md`);

  await Promise.all([
    fs.writeFile(jsonPath, serializeJsonReport(report), "utf8"),
    fs.writeFile(markdownPath, buildMarkdownReport(report), "utf8"),
  ]);

  return { jsonPath, markdownPath };
}
```

- [ ] **Step 4: Run reporter tests**

Run:

```bash
npm test -- __tests__/eval-reporter.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add eval/lib/reporter.ts __tests__/eval-reporter.test.ts
git commit -m "feat: add llm eval reports"
```

---

### Task 5: Runner, CLI, And Verification

**Files:**
- Create: `eval/lib/runner.ts`
- Create: `scripts/eval-llm.ts`
- Create: `__tests__/eval-runner.test.ts`

- [ ] **Step 1: Write runner tests**

Create `__tests__/eval-runner.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";

import { runEvalHarness } from "@/eval/lib/runner";
import type { EvalFixture, EvalJudgeResult } from "@/eval/lib/types";
import type { GeneratedCard } from "@/lib/types";

const fixture = {
  id: "language-jlpt-vocabulary",
  title: "JLPT",
  job: {
    deckName: "Deck",
    inputType: "pasted_text",
    cardType: "basic",
    requestedCardCount: 2,
    language: "ja+zh",
    studyPurpose: "language",
    guideProfile: "language.general",
    guideSelections: {
      learningLanguage: "ja",
      learningLanguageCustom: "",
      nativeLanguage: "zh",
      nativeLanguageCustom: "",
      targetExam: "jlpt_n4",
      targetExamCustom: "",
      taskType: "vocabulary",
      translationDirection: "learning_to_native",
    },
    strategyVersion: "language-v1",
    examSubject: null,
    examStage: null,
    sourceMode: "materials_notes",
    aiInstructions: "",
    userTags: ["eval", "language", "jlpt-n4"],
  },
  source: {
    name: "source",
    ref: "source · sample",
    text: "相談する＝困ったことについて人に意見を聞く。",
  },
  expectedCardCount: { min: 1, max: 3 },
  expectedTags: ["language", "jlpt-n4"],
  maxFrontLength: 120,
  maxBackLength: 220,
  domainChecks: ["basic_schema", "count_range", "source_ref", "expected_tags"],
  rubricNotes: ["Focused vocabulary cards."],
} as EvalFixture;

const cards: GeneratedCard[] = [
  {
    front: "相談するとは？",
    back: "困ったことについて人に意見を聞くこと。",
    tags: ["eval", "language", "jlpt-n4"],
    qualityFlags: [],
    sourceRef: "source · sample",
    confidence: 0.9,
  },
];

const judgeResult: EvalJudgeResult = {
  provider: "openai",
  model: "gpt-4.1-mini",
  scores: {
    atomicity: 90,
    grounding: 90,
    learnerFit: 85,
    usefulness: 85,
    domainFit: 90,
  },
  overallScore: 88,
  reasons: ["Focused."],
  suggestions: [],
};

describe("runEvalHarness", () => {
  it("runs fixtures with injected generation and judge providers", async () => {
    const report = await runEvalHarness({
      mode: "local",
      fixtures: [fixture],
      generationProvider: {
        name: "openai",
        generateCardsForChunk: vi.fn().mockResolvedValue(cards),
      },
      judgeCards: vi.fn().mockResolvedValue(judgeResult),
      now: () => new Date("2026-05-26T00:00:00.000Z"),
    });

    expect(report.runId).toBe("eval-2026-05-26T00-00-00-000Z");
    expect(report.results[0].status).toBe("pass");
    expect(report.results[0].totalScore).toBe(88);
    expect(report.overallAverage).toBe(88);
  });

  it("marks strict-mode low score as fail", async () => {
    const report = await runEvalHarness({
      mode: "strict",
      fixtures: [fixture],
      generationProvider: {
        name: "openai",
        generateCardsForChunk: vi.fn().mockResolvedValue(cards),
      },
      judgeCards: vi.fn().mockResolvedValue({ ...judgeResult, overallScore: 70 }),
      now: () => new Date("2026-05-26T00:00:00.000Z"),
    });

    expect(report.results[0].status).toBe("fail");
    expect(report.overallAverage).toBe(70);
  });
});
```

- [ ] **Step 2: Run runner tests and verify they fail**

Run:

```bash
npm test -- __tests__/eval-runner.test.ts
```

Expected: FAIL because `@/eval/lib/runner` does not exist yet.

- [ ] **Step 3: Implement runner**

Create `eval/lib/runner.ts`:

```ts
import { checkGeneratedCards } from "@/eval/lib/rule-checker";
import type {
  EvalCaseResult,
  EvalFixture,
  EvalGenerationProvider,
  EvalJudgeResult,
  EvalMode,
  EvalReport,
} from "@/eval/lib/types";
import { getCardGenerationProvider } from "@/lib/llm-provider";
import type { ContentChunk, JobRecord } from "@/lib/types";

const FIXTURE_SCORE_THRESHOLD = 75;
const OVERALL_SCORE_THRESHOLD = 80;

function runIdFromDate(date: Date) {
  return `eval-${date.toISOString().replace(/[:.]/g, "-")}`;
}

function makeJobRecord(fixture: EvalFixture, now: Date): JobRecord {
  return {
    id: `eval_${fixture.id}`,
    status: "generating",
    inputType: fixture.job.inputType,
    cardType: fixture.job.cardType,
    deckName: fixture.job.deckName,
    requestedCardCount: fixture.job.requestedCardCount,
    language: fixture.job.language,
    studyPurpose: fixture.job.studyPurpose,
    guideProfile: fixture.job.guideProfile,
    guideSelections: fixture.job.guideSelections,
    strategyVersion: fixture.job.strategyVersion,
    examSubject: fixture.job.examSubject,
    examStage: fixture.job.examStage,
    sourceMode: fixture.job.sourceMode,
    aiInstructions: fixture.job.aiInstructions,
    userTags: fixture.job.userTags,
    errorMessage: null,
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
    completedAt: null,
    sourceCount: 1,
    cardCount: 0,
    artifactId: null,
    artifactFormat: null,
    artifactPath: null,
  };
}

function makeChunk(fixture: EvalFixture): ContentChunk {
  return {
    id: `chunk_${fixture.id}`,
    sourceName: fixture.source.name,
    sourceRef: fixture.source.ref,
    text: fixture.source.text,
    charCount: fixture.source.text.length,
  };
}

function caseStatus(params: {
  mode: EvalMode;
  hardRulePassed: boolean;
  score: number | null;
  errorMessage: string | null;
}): EvalCaseResult["status"] {
  if (params.errorMessage) {
    return params.mode === "strict" ? "fail" : "warn";
  }

  if (!params.hardRulePassed) {
    return params.mode === "strict" ? "fail" : "warn";
  }

  if (params.score !== null && params.score < FIXTURE_SCORE_THRESHOLD) {
    return params.mode === "strict" ? "fail" : "warn";
  }

  return "pass";
}

function average(scores: number[]) {
  if (scores.length === 0) {
    return null;
  }

  return Math.round(scores.reduce((sum, score) => sum + score, 0) / scores.length);
}

export async function runEvalHarness(params: {
  mode: EvalMode;
  fixtures: EvalFixture[];
  generationProvider?: EvalGenerationProvider;
  judgeCards: (input: { fixture: EvalFixture; cards: Awaited<ReturnType<EvalGenerationProvider["generateCardsForChunk"]>> }) => Promise<EvalJudgeResult>;
  now?: () => Date;
}): Promise<EvalReport> {
  const now = params.now?.() ?? new Date();
  const generationProvider = params.generationProvider ?? getCardGenerationProvider();
  const results: EvalCaseResult[] = [];

  for (const fixture of params.fixtures) {
    let result: EvalCaseResult;

    try {
      const job = makeJobRecord(fixture, now);
      const chunk = makeChunk(fixture);
      const cards = await generationProvider.generateCardsForChunk({
        job,
        chunk,
        targetCardCount: fixture.job.requestedCardCount ?? fixture.expectedCardCount.max,
      });
      const ruleResult = checkGeneratedCards(fixture, cards);
      const judgeResult = await params.judgeCards({ fixture, cards });
      const status = caseStatus({
        mode: params.mode,
        hardRulePassed: ruleResult.passed,
        score: judgeResult.overallScore,
        errorMessage: null,
      });

      result = {
        fixture,
        cards,
        ruleResult,
        judgeResult,
        status,
        totalScore: judgeResult.overallScore,
        errorMessage: null,
      };
    } catch (error) {
      result = {
        fixture,
        cards: [],
        ruleResult: { passed: false, issues: [] },
        judgeResult: null,
        status: params.mode === "strict" ? "fail" : "skipped",
        totalScore: null,
        errorMessage: error instanceof Error ? error.message : "Unknown eval error.",
      };
    }

    results.push(result);
  }

  const scoredResults = results
    .map((result) => result.totalScore)
    .filter((score): score is number => score !== null);
  const overallAverage = average(scoredResults);
  const strictOverallFail =
    params.mode === "strict" &&
    overallAverage !== null &&
    overallAverage < OVERALL_SCORE_THRESHOLD;

  return {
    runId: runIdFromDate(now),
    mode: params.mode,
    createdAt: now.toISOString(),
    generationProvider: generationProvider.name,
    judgeProvider: results[0]?.judgeResult?.provider ?? "unknown",
    judgeModel: results[0]?.judgeResult?.model ?? null,
    overallAverage,
    results: strictOverallFail
      ? results.map((result) =>
          result.status === "pass" ? { ...result, status: "warn" as const } : result,
        )
      : results,
  };
}
```

- [ ] **Step 4: Run runner tests**

Run:

```bash
npm test -- __tests__/eval-runner.test.ts
```

Expected: PASS.

- [ ] **Step 5: Implement CLI entrypoint**

Create `scripts/eval-llm.ts`:

```ts
import { loadEvalFixtures } from "@/eval/lib/fixtures";
import { judgeGeneratedCards, resolveJudgeConfig } from "@/eval/lib/judge";
import { buildTerminalSummary, writeEvalReports } from "@/eval/lib/reporter";
import { runEvalHarness } from "@/eval/lib/runner";
import { getServerEnv } from "@/lib/env";
import { getCardGenerationProvider } from "@/lib/llm-provider";
import type { LlmProvider } from "@/lib/types";

function hasApiKey(provider: LlmProvider) {
  const env = getServerEnv();
  return provider === "openai" ? Boolean(env.openAiApiKey) : Boolean(env.geminiApiKey);
}

async function main() {
  const strict = process.argv.includes("--strict") || process.env.RUN_LLM_EVAL === "1";
  const mode = strict ? "strict" : "local";
  const env = getServerEnv();
  const generationProvider = getCardGenerationProvider();
  const appModel = env.llmProvider === "openai" ? env.openAiModel : env.geminiModel;
  const judgeConfig = resolveJudgeConfig({
    appProvider: env.llmProvider,
    appModel,
  });

  if (!hasApiKey(generationProvider.name) || !hasApiKey(judgeConfig.provider)) {
    const message = [
      "LLM eval skipped because required API keys are missing.",
      `Generation provider: ${generationProvider.name}`,
      `Judge provider: ${judgeConfig.provider}`,
      "Set OPENAI_API_KEY or GEMINI_API_KEY as needed.",
    ].join("\n");

    console.log(message);

    if (strict) {
      process.exitCode = 1;
    }

    return;
  }

  const report = await runEvalHarness({
    mode,
    fixtures: loadEvalFixtures(),
    generationProvider,
    judgeCards: ({ fixture, cards }) =>
      judgeGeneratedCards({
        fixture,
        cards,
        config: judgeConfig,
      }),
  });

  const paths = await writeEvalReports(report);
  console.log(buildTerminalSummary(report));
  console.log("");
  console.log(`JSON report: ${paths.jsonPath}`);
  console.log(`Markdown report: ${paths.markdownPath}`);

  const hasFailure = report.results.some((result) => result.status === "fail");

  if (strict && hasFailure) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
```

- [ ] **Step 6: Run focused eval tests**

Run:

```bash
npm test -- __tests__/eval-fixtures.test.ts __tests__/eval-rule-checker.test.ts __tests__/eval-judge.test.ts __tests__/eval-reporter.test.ts __tests__/eval-runner.test.ts
```

Expected: PASS.

- [ ] **Step 7: Run local eval command without API keys**

Run:

```bash
npm run eval:llm
```

Expected when API keys are absent: command prints that LLM eval was skipped and exits 0.
Expected when API keys are present: command writes JSON and Markdown reports to `eval/reports/`.

- [ ] **Step 8: Run strict eval command without API keys**

Run:

```bash
npm run eval:llm:ci
```

Expected when API keys are absent: command prints that LLM eval was skipped and exits 1.

- [ ] **Step 9: Run full verification**

Run:

```bash
npm test
npm run check
npm run build
```

Expected: all commands PASS.

- [ ] **Step 10: Commit Task 5**

```bash
git add eval/lib/runner.ts scripts/eval-llm.ts __tests__/eval-runner.test.ts
git commit -m "feat: add llm eval runner"
```

---

## Self-Review Notes

- Spec coverage: The plan covers CLI commands, seven fixtures, deterministic checks, LLM judge with independent provider config, JSON and Markdown reports, report gitignore, local versus strict behavior, and non-LLM unit tests.
- Scope: The plan does not add UI, dashboards, model leaderboards, or real student content.
- Type consistency: The shared `EvalFixture`, `EvalRuleResult`, `EvalJudgeResult`, and `EvalReport` types are introduced before all modules that consume them.
- Execution risk: The only live LLM behavior is in `npm run eval:llm` and `npm run eval:llm:ci`; all `npm test` coverage uses fixtures or injected mocks.
