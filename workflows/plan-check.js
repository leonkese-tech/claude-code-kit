export const meta = {
  name: 'plan-check',
  description: '계획·아이디어 점검 — 부수기·가정 검증·근거/결정 충돌 세 관점을 따로 돌리고, 한 줄 요약·핵심 3~5·다음 행동 하나로 모은다(읽기 전용)',
  whenToUse: '비용·시간이 크게 드는 결정 직전, 아이디어·전략·자동화 설계·규칙 변경안을 «동의 없이» 점검할 때. args = {plan: "계획 본문", context?: "배경·제약", goal?: "무엇을 이루려는가"}',
  phases: [
    { title: 'Attack', detail: '세 관점이 서로 모르게 따로 본다' },
    { title: 'Summarize', detail: '고정 형식으로 모은다' },
  ],
}

// 한 모델이 스스로 채점하는 대신 **서로 모르는 세 관점**(부수기·가정 검증·근거/결정 충돌)이 따로 보고,
// 모으는 쪽은 새로 판단하지 않고 고정 형식(한 문장 요약·핵심 3~5·다음 행동 하나)으로만 정리한다.
// 읽기 전용(Plan 에이전트) — 파일을 고치거나 발송·등록하지 않는다.

const a = args || {}
const plan = typeof a === 'string' ? a : a.plan
if (!plan) throw new Error('args = {plan, context?, goal?} 가 필요합니다')
const ctx = `점검 대상 계획:
${plan}
${a.goal ? `\n이루려는 것: ${a.goal}` : ''}${a.context ? `\n배경·제약: ${a.context}` : ''}

공통 규칙: 칭찬·장점·총평을 쓰지 마라. 돌려 말하지 마라. 확인하지 못한 것은 추측으로 채우지 말고 «미확인» 으로 둔다.
프로젝트 자료가 필요하면 저장소의 문서(CLAUDE.md, docs/ 등)를 grep 으로 찾아 근거로 쓴다(통째로 읽지 말 것).`

const ITEM = {
  type: 'object',
  properties: {
    point: { type: 'string' },
    why: { type: 'string' },
    severity: { type: 'string', enum: ['치명', '큼', '보통'] },
    evidence: { type: 'string' },
  },
  required: ['point', 'why', 'severity', 'evidence'],
}
const FIND = { type: 'object', properties: { items: { type: 'array', items: ITEM }, unknown: { type: 'array', items: { type: 'string' } } }, required: ['items', 'unknown'] }
const ASSUME = {
  type: 'object',
  properties: {
    assumptions: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          assumption: { type: 'string' },
          if_wrong: { type: 'string' },
          changes_recommendation: { type: 'string' },
          how_to_check: { type: 'string' },
          risk: { type: 'string', enum: ['치명', '큼', '보통'] },
        },
        required: ['assumption', 'if_wrong', 'changes_recommendation', 'how_to_check', 'risk'],
      },
    },
  },
  required: ['assumptions'],
}

phase('Attack')
const [attack, assume, facts] = await parallel([
  () => agent(`${ctx}

너의 유일한 임무는 이 계획을 부수는 것이다. 결함 있는 가정, 놓친 위험, 2차 효과, 실패할 수 있는 지점을 전부 찾아라.
각 항목에 왜 무너지는지와 근거(자료·사례·계산)를 적어라. 근거가 없으면 evidence 에 «추론» 이라고 적어라.`,
    { agentType: 'Plan', phase: 'Attack', label: '부수기', schema: FIND }),
  () => agent(`${ctx}

이 계획이 현실에서 확인해야 할 가정을 전부 나열하라. 각 가정마다 (a) 틀리면 무슨 일이 생기는지 (b) 그러면 추천이 어떻게 바뀌는지
(c) 무엇을 보면 확인되는지(가장 싼 확인 방법)를 적어라. 계획에 적혀 있지 않지만 암묵적으로 깔린 가정도 찾아라.`,
    { agentType: 'Plan', phase: 'Attack', label: '가정 검증', schema: ASSUME }),
  () => agent(`${ctx}

사실·범위 점검만 하라: 계획 안의 주장·숫자·전제 중 근거가 없거나 틀린 것, 그리고 프로젝트의 확정 결정·규칙(CLAUDE.md 등 저장소 문서)과
부딪히는 것을 찾아라. 자료에 없는 것은 «자료에 없음» 이라고 적고 멈춰라 — 그럴듯하게 메우지 마라.`,
    { agentType: 'Plan', phase: 'Attack', label: '근거·결정 충돌', schema: FIND }),
])
const got = [attack, assume, facts].filter(Boolean).length
if (got < 3) log(`관점 ${3 - got}개가 결과 없이 끝났습니다 — 요약에 그 사실을 적습니다`)

phase('Summarize')
const summary = await agent(`아래는 한 계획을 서로 모르는 세 관점이 따로 본 결과다. **새로 판단하지 말고** 이것만으로 정리하라.
칭찬·총평 금지. 정확히 이 구조로만, 한국어 존댓말로 써라(다른 말 붙이지 말 것):

1. 한 문장 요약 — 이 계획이 지금 상태로 가면 어떻게 되는가
2. 핵심 3~5개(글머리) — 치명·큼 우선, 각 줄 끝에 (부수기)/(가정)/(근거) 출처 표시
3. 지금 할 다음 행동 하나 — 가장 싼 확인 또는 가장 큰 위험을 줄이는 한 가지

${got < 3 ? `⚠️ 관점 ${3 - got}개가 결과 없이 끝났다 — 요약 맨 끝에 «일부 관점 누락» 이라고 적어라.\n` : ''}
부수기: ${JSON.stringify(attack)}
가정 검증: ${JSON.stringify(assume)}
근거·결정 충돌: ${JSON.stringify(facts)}`,
  { agentType: 'Plan', phase: 'Summarize', label: '요약(형식 고정)' })

return { summary, attack, assumptions: assume, facts, lenses_ok: got }
