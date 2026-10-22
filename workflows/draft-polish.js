export const meta = {
  name: 'draft-polish',
  description: '완성도가 중요한 글 다듬기 — 초안 → 독립 비평자 10점 채점 → 8점 미만이면 약점만 고쳐 다시(최대 3회) → 최종본과 무엇이 나아졌는지',
  whenToUse: '고객 메일·보고서 문안·소개 문구·기획서 글처럼 완성도가 중요한 «글» 을 쓸 때(파일은 만들지 않는다 — 글만 돌려준다). args = {task: "무엇을 쓰나", material?: "근거 자료·사실", audience?: "누가 읽나", tone?: "말투", max_len?: 글자 수}',
  phases: [
    { title: 'Draft', detail: '초안' },
    { title: 'Critique', detail: '쓴 쪽이 아닌 비평자가 채점' },
    { title: 'Revise', detail: '약점만 고친다' },
  ],
}

// 자기 채점은 관대하다. 그래서 **쓴 쪽과 채점하는 쪽을 나눈다.** 채점 기준에 «자료에 없는 사실 금지»(지어낸 사실이 있으면 최대 4점)를 넣는다.
// 파일을 만들지 않는다 — 글만 돌려주고, 저장·발송은 부른 쪽이 한다.

const a = typeof args === 'string' ? { task: args } : (args || {})
if (!a.task) throw new Error('args = {task, material?, audience?, tone?, max_len?} 가 필요합니다')
const BRIEF = `쓸 글: ${a.task}
${a.audience ? `읽는 사람: ${a.audience}\n` : ''}${a.tone ? `말투: ${a.tone}\n` : '말투: 정중한 한국어 존댓말·문서체(대화체·상투어·과장 금지)\n'}${a.max_len ? `길이 상한: ${a.max_len}자\n` : ''}${a.material ? `근거 자료(이 안의 사실만 쓴다):\n${a.material}\n` : '근거 자료 없음 — 사실·숫자·고유명사를 지어내지 말고, 필요한 자리는 [확인 필요] 로 비워 둔다.\n'}`
const SCORE = {
  type: 'object',
  properties: {
    score: { type: 'number' },
    fatal: { type: 'array', items: { type: 'string' } },
    weaknesses: { type: 'array', items: { type: 'string' } },
    invented_facts: { type: 'array', items: { type: 'string' } },
  },
  required: ['score', 'fatal', 'weaknesses', 'invented_facts'],
}
const RUBRIC = `10점 만점 채점 기준(엄격하게 — 무난하면 6점):
- 읽는 사람이 무엇을 하면 되는지 첫머리에서 바로 보이는가(3)
- 자료에 없는 사실·숫자·약속을 지어냈는가 — 하나라도 있으면 최대 4점(invented_facts 에 적는다)(2)
- 과장·단정(«완벽»·«100%»·«무조건»)·상투어·AI 티 나는 문장이 있는가(2)
- 군더더기 없이 짧은가·구조가 읽기 쉬운가(2)
- 말투가 요구와 맞는가(1)
칭찬·총평은 쓰지 마라. 점수와 고칠 것만.`

// ⚠️ 메일 문안 같은 글을 쓰면 하네스 안전 검사가 초안 결과 앞에 «SECURITY WARNING: …» 한 줄을 붙일 수 있다(실제 도구 호출이 없어도).
//    그 문장이 본문에 섞여 편집자가 치명으로 채점하므로, 경고를 **숨기지 않고** 본문에서 떼어 결과의 harness_warnings 에 남긴다.
const harnessWarnings = []
function clean(text, label) {
  let t = String(text || '')
  const m = t.match(/^\s*SECURITY WARNING:[^\n]*(\n+|$)/)
  if (m) { harnessWarnings.push({ step: label, text: m[0].trim() }); t = t.slice(m[0].length) }
  return t.trim()
}

phase('Draft')
let draft = await agent(`${BRIEF}
위 글의 초안을 써라. 글만 돌려주고 설명·머리말은 붙이지 마라. 파일을 만들거나 고치지 마라.`,
  { agentType: 'Plan', phase: 'Draft', label: '초안' })
draft = clean(draft, '초안')
const history = []
for (let r = 1; r <= 3; r++) {
  phase('Critique')
  const c = await agent(`너는 이 글을 쓰지 않은 편집자다. 아래 요구와 초안을 보고 채점하라.
${BRIEF}
${RUBRIC}

초안:
${draft}`, { agentType: 'Plan', phase: 'Critique', label: `채점${r}`, schema: SCORE })
  if (!c) { history.push({ round: r, score: null, note: '채점 실패 — 판정 불가' }); break }
  history.push({ round: r, score: c.score, fatal: c.fatal, weaknesses: c.weaknesses, invented_facts: c.invented_facts })
  log(`${r}회차 점수 ${c.score}/10 · 치명 ${c.fatal.length} · 지어낸 사실 ${c.invented_facts.length}`)
  if (c.score >= 8 && !c.fatal.length && !c.invented_facts.length) break
  if (r === 3) break
  phase('Revise')
  const nd = await agent(`${BRIEF}
편집자가 아래를 지적했다. **지적된 약점만** 고쳐 다시 써라(잘된 부분은 건드리지 말 것). 지어낸 사실은 지우거나 [확인 필요] 로 바꿔라.
글만 돌려준다.

치명: ${c.fatal.join(' / ') || '없음'}
약점: ${c.weaknesses.join(' / ') || '없음'}
지어낸 사실: ${c.invented_facts.join(' / ') || '없음'}

지금 글:
${draft}`, { agentType: 'Plan', phase: 'Revise', label: `고쳐쓰기${r}` })
  if (!nd) break
  draft = clean(nd, `고쳐쓰기${r}`)
}
const last = history[history.length - 1] || {}
return {
  final: draft,
  final_score: last.score ?? null,
  passed: last.score != null && last.score >= 8 && !(last.fatal || []).length && !(last.invented_facts || []).length,
  rounds: history,
  harness_warnings: harnessWarnings,
  note: '점수는 쓴 쪽이 아닌 편집자 채점. passed=false 면 사람이 보고 고친다. 발송·저장은 하지 않았다.',
}
