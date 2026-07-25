# WordsWave Branding Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all user-facing CAD Agent branding with the provided WordsWave logo and name without changing product behavior.

**Architecture:** Store the provided JPEG as a Vite-managed source asset and render it through one reusable `BrandMark` component. Use explicit labeled and compact variants so the start page, login page, and project header share image loading, fallback, spacing, and accessibility behavior.

**Tech Stack:** React 19, TypeScript, Vite 8, Tailwind CSS, Node test runner.

---

## File map

- Create `frontend/src/assets/wordswave-logo.jpg`: repository-owned copy of the provided brand asset.
- Create `frontend/src/components/common/BrandMark.tsx`: the only component that renders the product logo and name.
- Create `frontend/tests/branding.test.ts`: source and asset contract tests for branding coverage.
- Modify `frontend/src/components/project/ProjectStart.tsx`: labeled brand in the start-page header.
- Modify `frontend/src/components/project/WorkspaceHeader.tsx`: compact logo while retaining the current project name.
- Modify `frontend/src/components/LoginPage.tsx`: labeled brand on the authentication page.
- Modify `frontend/src/index.css`: remove the obsolete letter-mark style.
- Modify `frontend/index.html`: WordsWave title, description, and favicon.

## Execution baseline

Before Task 1, run `git status --short` and record the current commit with `git rev-parse HEAD`. Commit this reviewed plan before implementation so the code-change baseline is clean. At final review, compare every changed path after that baseline against the file map above; pre-existing spec/plan commits are not treated as product-code changes.

### Task 1: Add the brand contract test

**Files:**
- Create: `frontend/tests/branding.test.ts`

- [ ] **Step 1: Write the failing test**

Create a Node test that:

```ts
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

test("WordsWave branding is centralized and covers every product surface", () => {
  assert.equal(existsSync(join(FRONTEND, "src/assets/wordswave-logo.jpg")), true);

  const brand = read("src/components/common/BrandMark.tsx");
  assert.match(brand, /wordswave-logo\.jpg/);
  assert.match(brand, /WordsWave/);
  assert.match(brand, /object-contain/);
  assert.match(brand, /onError/);
  assert.match(brand, /h-6 w-6/);
  assert.match(brand, /h-8 w-8/);

  assert.match(read("src/components/project/ProjectStart.tsx"), /<BrandMark/);
  assert.match(read("src/components/project/WorkspaceHeader.tsx"), /<BrandMark[^>]*showName=\{false\}/);
  assert.match(read("src/components/LoginPage.tsx"), /<BrandMark/);

  const html = read("index.html");
  assert.match(html, /<title>WordsWave \| AI 工程工作台<\/title>/);
  assert.match(html, /rel="icon"/);
});

test("live frontend surfaces no longer render the old product name", () => {
  for (const path of [
    "index.html",
    "src/components/project/ProjectStart.tsx",
    "src/components/project/WorkspaceHeader.tsx",
    "src/components/LoginPage.tsx",
  ]) {
    assert.equal(read(path).includes("CAD Agent"), false, path);
  }
});
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```bash
cd frontend
node --test --experimental-strip-types tests/branding.test.ts
```

Expected: FAIL because the logo asset and `BrandMark.tsx` do not exist.

- [ ] **Step 3: Commit the failing test**

```bash
git add frontend/tests/branding.test.ts
git commit -m "test: define WordsWave branding contract"
```

### Task 2: Add the logo asset and reusable component

**Files:**
- Create: `frontend/src/assets/wordswave-logo.jpg`
- Create: `frontend/src/components/common/BrandMark.tsx`

- [ ] **Step 1: Copy the provided asset without transforming it**

Copy `/Users/wentao/Desktop/Owen/wordswave_logo.jpg` to `frontend/src/assets/wordswave-logo.jpg`. Verify the copy remains a 1267 × 1280 JPEG.

- [ ] **Step 2: Implement `BrandMark`**

```tsx
import { useState } from "react";
import logoUrl from "../../assets/wordswave-logo.jpg";

interface BrandMarkProps {
  className?: string;
  showName?: boolean;
  size?: "default" | "login";
  tone?: "default" | "inverse";
}

export function BrandMark({
  className = "",
  showName = true,
  size = "default",
  tone = "default",
}: BrandMarkProps) {
  const [failed, setFailed] = useState(false);
  const markSize = size === "login" ? "h-8 w-8 rounded-lg" : "h-6 w-6 rounded-lg";
  const fallbackTone = tone === "inverse"
    ? "bg-white text-slate-950"
    : "bg-[var(--ink)] text-white";

  return (
    <span
      aria-label={showName ? undefined : "WordsWave"}
      className={`inline-flex min-w-0 items-center gap-2.5 ${className}`}
      role={showName ? undefined : "img"}
    >
      {failed ? (
        <span aria-hidden="true" className={`grid shrink-0 place-items-center text-xs font-semibold ${markSize} ${fallbackTone}`}>W</span>
      ) : (
        <img
          alt=""
          aria-hidden="true"
          className={`shrink-0 object-contain ${markSize}`}
          onError={() => setFailed(true)}
          src={logoUrl}
        />
      )}
      {showName ? <span className="font-semibold">WordsWave</span> : null}
    </span>
  );
}
```

- [ ] **Step 3: Run lint and typecheck**

Run:

```bash
cd frontend
npm run lint
npx tsc --noEmit -p tsconfig.app.json
```

Expected: PASS.

- [ ] **Step 4: Commit the component and asset**

```bash
git add frontend/src/assets/wordswave-logo.jpg frontend/src/components/common/BrandMark.tsx
git commit -m "feat: add reusable WordsWave brand mark"
```

### Task 3: Replace every live brand surface

**Files:**
- Modify: `frontend/src/components/project/ProjectStart.tsx`
- Modify: `frontend/src/components/project/WorkspaceHeader.tsx`
- Modify: `frontend/src/components/LoginPage.tsx`
- Modify: `frontend/src/index.css`
- Modify: `frontend/index.html`

- [ ] **Step 1: Replace the start-page brand**

Import `BrandMark` and replace the letter mark plus `CAD Agent` text with:

```tsx
<BrandMark className="text-sm" />
```

- [ ] **Step 2: Replace the workspace mark**

Import `BrandMark` and replace `<span className="workspace-brand">C</span>` with:

```tsx
<BrandMark showName={false} />
```

Keep the existing project-name span unchanged.

- [ ] **Step 3: Replace the login-page brand**

Remove the box icon mark and render:

```tsx
<BrandMark className="text-white" size="login" tone="inverse" />
```

Keep “参数化建模工作台” as the subtitle.

- [ ] **Step 4: Update document metadata**

Set:

```html
<link rel="icon" type="image/jpeg" href="/src/assets/wordswave-logo.jpg" />
<meta name="description" content="WordsWave AI 工程工作台" />
<title>WordsWave | AI 工程工作台</title>
```

- [ ] **Step 5: Remove obsolete CSS**

Delete the `.workspace-brand` rule from `frontend/src/index.css`; no other design tokens change.

- [ ] **Step 6: Run the focused brand test**

Run:

```bash
cd frontend
node --test --experimental-strip-types tests/branding.test.ts
```

Expected: PASS.

- [ ] **Step 7: Commit the integration**

```bash
git add frontend/index.html frontend/src/index.css frontend/src/components/LoginPage.tsx frontend/src/components/project/ProjectStart.tsx frontend/src/components/project/WorkspaceHeader.tsx
git commit -m "feat: apply WordsWave branding"
```

### Task 4: Verify the production result

**Files:**
- Test: `frontend/tests/*.test.ts`
- Build output: `frontend/dist/`

- [ ] **Step 1: Run the complete frontend gate**

Run:

```bash
cd frontend
npm run lint
node --test --experimental-strip-types tests/*.test.ts
npx tsc --noEmit -p tsconfig.app.json
npx vite build
```

Expected: all checks pass; the only accepted warning is the pre-existing Viewer3D chunk-size warning.

- [ ] **Step 2: Preview the production build**

Run `npx vite preview --host 127.0.0.1 --port 4173` and verify:

- start page shows logo + WordsWave;
- authenticated workspace shows logo + project name;
- authentication-enabled backend shows logo + WordsWave on login;
- `document.title` is `WordsWave | AI 工程工作台`;
- favicon and logo requests return 200;
- 1440px, 1280px, and 375px layouts do not overflow;
- the browser console contains no new errors.

- [ ] **Step 3: Simulate image failure**

Block or replace the image URL in browser devtools and confirm:

- labeled variants keep visible `WordsWave`;
- compact workspace variant displays `W`;
- header dimensions do not shift.

- [ ] **Step 4: Inspect the final diff**

Run `git diff --check`, then inspect both the complete changed-path list since the recorded implementation baseline and the path-scoped product diff:

```bash
git diff --check
git diff --name-only <IMPLEMENTATION_BASE>..HEAD
git diff <IMPLEMENTATION_BASE>..HEAD -- \
  frontend/index.html \
  frontend/src/index.css \
  frontend/src/assets/wordswave-logo.jpg \
  frontend/src/components/common/BrandMark.tsx \
  frontend/src/components/LoginPage.tsx \
  frontend/src/components/project/ProjectStart.tsx \
  frontend/src/components/project/WorkspaceHeader.tsx \
  frontend/tests/branding.test.ts
git status --short
```

Expected: no whitespace errors, a clean worktree, and the complete changed-path list contains only the eight planned frontend paths above. The path-scoped diff contains no business-logic, API, authentication, WebSocket, or data-model changes.
