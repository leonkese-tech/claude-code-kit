export const meta = {
  name: 'expert-task-generic',
  description: 'Specialist implements -> independent reviewers re-read and reproduce -> specialist fixes -> re-review (max 3 rounds). A reviewer that returns nothing is review_incomplete, never clean.',
  whenToUse: 'Use when a change must not be accepted on the author\'s own "tests pass". args = {task, agentDefinitionPath?, specialist?, risk?: "high"|"normal"|"low", lensesByRisk?, domainLens?, collaborators?: [{name, lens}], maxRounds?, checkCommand?, reviewer?, specialistModel?, reviewerModel?, extraRules?}',
  phases: [
    { title: 'Implement', detail: 'The specialist makes the change, runs its own checks, lists unverified assumptions' },
    { title: 'Review', detail: 'Independent read-only reviewers, each with a different lens' },
    { title: 'Fix', detail: 'The specialist fixes only the defects it can reproduce' },
  ],
}

// Why this exists: a fixer's own "tests pass" cannot see the side effects of its own fix. Reviewers are
// separate agents with a different lens, they re-read the diff and try to reproduce, and they start from the
// assumptions the author admitted it did not verify. See expert-task-generic.md.

// ---- Configuration (everything can be overridden through args) -------------------------------------------
const a = args || {}
if (!a.task) throw new Error('args.task is required')

const VALID_RISK = ['high', 'normal', 'low']
if (a.risk != null && !VALID_RISK.includes(a.risk)) {
  throw new Error(`unknown risk ${JSON.stringify(a.risk)}; valid values: ${VALID_RISK.join(', ')} (omit it for "normal")`)
}
const risk = a.risk == null ? 'normal' : a.risk
// Default 2 rounds for every risk level, so even "low" gets review -> fix -> re-review. maxRounds: 1 means
// review only (defects then end as defects_remain with no fix phase).
const maxRounds = Math.min(3, Math.max(1, Math.floor(Number(a.maxRounds) || 2)))

// Which review lenses run per risk level. Lens names: common | domain | adversarial | collaborators.
const DEFAULT_LENSES = {
  low: ['common'],
  normal: ['common', 'domain'],
  high: ['common', 'domain', 'adversarial'],
}
const lenses = (a.lensesByRisk && a.lensesByRisk[risk]) || DEFAULT_LENSES[risk]
const collaborators = (a.collaborators || []).filter(c => c && c.name)

const LENS_TEXT = {
  common: 'Common checklist: look for defects this change newly introduced - regressions in neighbouring code, ' +
    'unhandled empty/invalid input, error paths that swallow failures silently, changed behaviour that no test covers, ' +
    'files touched that the summary does not mention, the same bug pattern left in sibling code.',
  domain: a.domainLens || 'Domain lens: judge the change as a user/operator of this area would - exercise the real ' +
    'entry point instead of calling internals, compare before/after outputs on realistic inputs, check that every ' +
    'path that should give the same answer still does.',
  adversarial: 'Adversarial lens: assume the change is WRONG and try to break it - boundary values, ' +
    'ordering and re-runs, concurrent use, corrupted or missing files, a different code path to the same function. ' +
    'If you cannot confirm something, put it in "unverified", not in "defects".',
}

// Validate the reviewer setup BEFORE the specialist runs (a typo must not be found after the commit).
if (!Array.isArray(lenses) || !lenses.length) {
  throw new Error(`no lenses configured for risk "${risk}" (lensesByRisk.${risk} must be a non-empty array)`)
}
for (const name of lenses) {
  if (name !== 'collaborators' && !LENS_TEXT[name]) {
    throw new Error(`unknown lens ${JSON.stringify(name)} (use common|domain|adversarial|collaborators)`)
  }
}
const useCollaborators = lenses.includes('collaborators') || risk !== 'low'
const reviewerCount = lenses.filter(n => n !== 'collaborators').length + (useCollaborators ? collaborators.length : 0)
if (reviewerCount < 1) {
  throw new Error('no reviewers configured: lenses ' + JSON.stringify(lenses) +
    ' resolve to zero reviewers (add a lens, or collaborators)')
}

const specialist = a.specialist || 'general-purpose'
const reviewerType = a.reviewer || 'general-purpose'
const SPECIALIST_OPTS = a.specialistModel ? { model: a.specialistModel } : {}
// One reviewer deliberately runs on a different model (blind spots differ). Set reviewerModel to enable.
const REVIEW_MODEL = a.reviewerModel
const CHECK = a.checkCommand
  ? `Project check command (run it, report the result): ${a.checkCommand}`
  : 'Run the project\'s own tests/lint for the files you touch and say exactly what you ran.'
const EXTRA = a.extraRules ? `\nProject rules:\n${a.extraRules}` : ''
const SAFE_TEST = '\nTests and reproductions must not send messages, write to production data, or call paid/external services. ' +
  'Stub those first; if you cannot confirm a stub, do not run the test.'
const HONEST = '\nReport rules: facts only, no praise. List what you did NOT do or are unsure about first. ' +
  'Record every assumption you relied on without verifying in "assumptions" (what breaks if it is wrong, how far you checked it).'

// If the named agent type is not registered, fall back to a general agent that reads the definition file first.
async function run(type, prompt, allOpts) {
  const { readOnly, ...opts } = allOpts || {}
  try {
    return await agent(prompt, { ...opts, agentType: type })
  } catch (e) {
    if (!/agent type .*not found/i.test(String((e && e.message) || e)) || !a.agentDefinitionPath) throw e
    log(`${type} not registered -> general-purpose reading ${a.agentDefinitionPath}`)
    const ro = readOnly ? '\nYou are a READ-ONLY reviewer: modify and commit nothing; put scratch files in the temp dir.' : ''
    return await agent(`First read ${a.agentDefinitionPath} completely and follow its role and rules.${ro}\n\n${prompt}`,
      { ...opts, agentType: 'general-purpose' })
  }
}

// A reviewer answer only counts as "looked" when it is well-formed, states what it covered (scope), and did not
// admit it could not run anything. Everything else is incomplete: unknown, never clean.
function assess(x) {
  if (!x || typeof x !== 'object' || Array.isArray(x)) return { ok: false, reason: 'no usable answer' }
  if (!Array.isArray(x.defects)) return { ok: false, reason: 'malformed answer (no defects array)' }
  if (x.defects.some(d => !d || typeof d !== 'object')) return { ok: false, reason: 'malformed defect entry' }
  const unverified = Array.isArray(x.unverified) ? x.unverified : []
  const tests = Array.isArray(x.tests) ? x.tests : []
  if (typeof x.scope !== 'string' || !x.scope.trim()) return { ok: false, reason: 'empty scope (did not say what was examined)' }
  if (unverified.length && !tests.length) return { ok: false, reason: 'ran nothing (unverified items but no tests)' }
  return { ok: true, value: { defects: x.defects, unverified, tests, scope: x.scope } }
}

const REVIEW = {
  type: 'object',
  properties: {
    defects: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['high', 'medium', 'low'] },
      where: { type: 'string' }, what: { type: 'string' }, repro: { type: 'string' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    }, required: ['severity', 'where', 'what', 'repro', 'confidence'] } },
    unverified: { type: 'array', items: { type: 'string' } },
    tests: { type: 'array', items: { type: 'string' } },
    scope: { type: 'string' },
  },
  required: ['defects', 'unverified', 'tests', 'scope'],
}

const WORK = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    files: { type: 'array', items: { type: 'string' } },
    tests: { type: 'array', items: { type: 'string' } },
    needs_user: { type: 'array', items: { type: 'string' } },
    assumptions: { type: 'array', items: { type: 'object', properties: {
      assumption: { type: 'string' }, if_wrong: { type: 'string' },
      checked: { type: 'string', enum: ['verified', 'partly', 'unverified'] },
    }, required: ['assumption', 'if_wrong', 'checked'] } },
    notes: { type: 'string' },
  },
  required: ['summary', 'commits', 'files', 'tests', 'needs_user', 'assumptions', 'notes'],
}

// ---- Implement / Review / Fix ----------------------------------------------------------------------------
// From the moment the specialist may have committed, no exception may lose the report: everything below runs in
// one try/catch that returns {status:'error', work: <last known report>, rounds} instead of throwing.
const rounds = []
const needsUser = []
let lastWork = null          // latest known specialist report (implement or a fix round)
const errMsg = e => String((e && e.message) || e)
const safeReturn = (status, extra) => ({ status, risk, work: lastWork, rounds, needs_user: needsUser, ...(extra || {}) })

try {
  phase('Implement')
  log(`specialist=${specialist} risk=${risk} lenses=${lenses.join('+')}${collaborators.length ? ' collaborators=' + collaborators.map(c => c.name).join(',') : ''}`)
  const work = await run(specialist,
    `Carry out the task below. When done, self-check it (${CHECK}) and commit only your own files
(if the index contains other people's staged files, commit just your paths and say so in notes).${EXTRA}${SAFE_TEST}${HONEST}

Task:
${a.task}`,
    { ...SPECIALIST_OPTS, phase: 'Implement', label: `${specialist}:implement`, schema: WORK })
  if (!work) return { status: 'implement_failed', risk, work: null, rounds: [], needs_user: [] }
  lastWork = work
  needsUser.push(...(work.needs_user || []))
  // Nothing changed -> a reviewer would find no defects and report "clean". That would be a false pass.
  if (!(work.commits || []).length && !(work.files || []).length) {
    log('no change produced -> no_change (not clean)')
    return { status: 'no_change', risk, work, rounds: [], needs_user: needsUser }
  }

  let current = work
  for (let r = 1; r <= maxRounds; r++) {
    const brief = `Review target: a change by "${specialist}" (risk ${risk}).
Summary: ${current.summary}
Commits: ${(current.commits || []).join(', ') || '(none)'}
Files: ${(current.files || []).join(', ')}
Tests the author claims: ${(current.tests || []).join(' / ')}
Assumptions the author relied on (attack the unverified ones FIRST): ${
      (current.assumptions || []).map(x => `[${x.checked}] ${x.assumption} -> if wrong: ${x.if_wrong}`).join(' / ') ||
      '(none listed - that omission is itself suspicious)'}
Original task: ${a.task}
${CHECK}${EXTRA}${SAFE_TEST}
You are read-only: do not modify or commit files. Read the actual diff yourself and reproduce before reporting.
Only report a defect you reproduced or can point to exactly; everything else goes to "unverified".`
    // Each reviewer runs in its own try/catch: one that throws must not discard the others' answers.
    const mk = (label, text, opts) => async () => {
      try {
        return { v: await run(reviewerType, `${brief}\n\n${text}`,
          { readOnly: true, phase: 'Review', label: `review${r}:${label}`, schema: REVIEW, ...(opts || {}) }) }
      } catch (e) {
        return { threw: errMsg(e) }
      }
    }

    const jobs = []
    const labels = []
    for (const name of lenses) {
      if (name === 'collaborators') continue
      const opts = {}
      // the domain reviewer runs on the alternative model when one is configured
      if (REVIEW_MODEL && name === 'domain') opts.model = REVIEW_MODEL
      if (name === 'adversarial') opts.effort = 'high'
      jobs.push(mk(name, LENS_TEXT[name], opts))
      labels.push(name)
    }
    if (useCollaborators) {
      for (const c of collaborators) {
        jobs.push(mk(`collab:${c.name}`,
          `Collaborator lens (${c.name}): check that this change did not break the rules of that area: ${c.lens || 'its owner\'s usual concerns'}`))
        labels.push(`collab:${c.name}`)
      }
    }

    const raw = await parallel(jobs)
    const checked = jobs.map((_, i) => {
      const x = raw && raw[i]
      if (x && x.threw) return { ok: false, reason: `reviewer threw: ${x.threw}` }
      return assess(x && x.v)
    })
    const got = checked.filter(c => c.ok).map(c => c.value)
    const incomplete = checked.map((c, i) => (c.ok ? null : { reviewer: labels[i], reason: c.reason })).filter(Boolean)
    const all = got.flatMap(x => x.defects)
    // Hand to the fixer: everything high/medium, plus low severity only if confidence is high.
    const defects = all.filter(d => d.severity !== 'low' || d.confidence === 'high')
    rounds.push({ round: r, reviewers: got.length, expected: jobs.length, incomplete, defects: all,
                  unverified: got.flatMap(x => x.unverified), reviewer_tests: got.flatMap(x => x.tests), scope: got.map(x => x.scope) })
    log(`round ${r}: reviewers ${got.length}/${jobs.length}, defects to fix ${defects.length}`)

    // A reviewer that returned nothing did not look: that is "unknown", never "clean".
    if (got.length < jobs.length) {
      log(`${incomplete.length} reviewer(s) incomplete (${incomplete.map(x => x.reviewer + ': ' + x.reason).join('; ')}) -> review_incomplete`)
      return safeReturn('review_incomplete', { work: current, pending_defects: defects })
    }
    // "clean" = nothing worth fixing; low-severity low-confidence defects may still sit in rounds[].defects.
    if (!defects.length) return safeReturn('clean', { work: current })
    if (r === maxRounds) break

    phase('Fix')
    let fixed = null
    try {
      fixed = await run(specialist,
        `Independent reviewers found the defects below in your change. Reproduce each one. If it is real, fix it and pin the
reproduction as a regression test. If you believe it is wrong, do not change anything - write your evidence in notes.
Afterwards run ${CHECK} and commit only your own files.${EXTRA}${SAFE_TEST}${HONEST}

${defects.map((d, i) => `${i + 1}. [${d.severity}/${d.confidence}] ${d.where} - ${d.what}\n   repro: ${d.repro}`).join('\n')}`,
        { ...SPECIALIST_OPTS, phase: 'Fix', label: `${specialist}:fix${r}`, schema: WORK })
    } catch (e) {
      // The fixer may have committed before throwing: say so instead of pretending the pre-fix report is current.
      return safeReturn('fix_failed', { work: current, error: errMsg(e), fixer_may_have_committed: true, pending_defects: defects })
    }
    if (!fixed || typeof fixed !== 'object') {
      return safeReturn('fix_failed', { work: current, error: 'fixer returned nothing usable', fixer_may_have_committed: true, pending_defects: defects })
    }
    // Latest known work = the fixer's report, with fields it left out taken from the previous report.
    const union = (x, y) => [...new Set([...(x || []), ...(y || [])])]
    current = { ...current, ...fixed, commits: union(current.commits, fixed.commits), files: union(current.files, fixed.files) }
    lastWork = current
    needsUser.push(...(fixed.needs_user || []))
  }
  return safeReturn('defects_remain', { work: current })
} catch (e) {
  log(`unexpected error: ${errMsg(e)}`)
  return safeReturn('error', { error: errMsg(e) })
}
