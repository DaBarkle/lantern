export const meta = {
  name: 'week-critics',
  description: 'Score a simulated Lantern week with three Opus critics, verify every weakness through two lenses, rank the surviving fixes',
  phases: [
    { title: 'Critique', detail: 'three independent Opus critics score the transcript' },
    { title: 'Merge', detail: 'one agent merges duplicate weaknesses' },
    { title: 'Verify', detail: 'two lenses per weakness: evidence and cause' },
    { title: 'Synthesize', detail: 'ranked fixes' },
  ],
}
const { transcript, head, plugin, rig } = args
const SCORES = { type: 'object', properties: { watches: { type: 'number' }, open_loops: { type: 'number' }, rules: { type: 'number' }, since_last: { type: 'number' }, sharper: { type: 'number' } }, required: ['watches', 'open_loops', 'rules', 'since_last', 'sharper'] }
const WEAK = { type: 'object', properties: { title: { type: 'string' }, evidence: { type: 'string', description: 'verbatim quote(s) from the transcript' }, fix: { type: 'string', description: 'concrete harness-level fix (code or instruction)' }, impact: { type: 'number', description: '1-10' } }, required: ['title', 'evidence', 'fix', 'impact'] }
const CRITIC = { type: 'object', properties: { scores: SCORES, weaknesses: { type: 'array', items: WEAK }, engineer: { type: 'string' }, non_technical: { type: 'string' } }, required: ['scores', 'weaknesses', 'engineer', 'non_technical'] }
const MERGED = { type: 'object', properties: { weaknesses: { type: 'array', items: WEAK } }, required: ['weaknesses'] }
const VERDICT = { type: 'object', properties: { real: { type: 'boolean' }, cause: { type: 'string', enum: ['harness', 'rig', 'model', 'not-real'] }, note: { type: 'string' }, file_hint: { type: 'string' } }, required: ['real', 'cause', 'note'] }

phase('Critique')
const critics = (await parallel([1, 2, 3].map(i => () => agent(
  `You are critic ${i} of 3, working independently. Read the file ${head} first: it is your full instruction (what happened on the machine, the five promises to score 1-10, then the five most important weaknesses each with a concrete harness-level fix, then what an expert engineer and a non-technical person would say). Then read the transcript file ${transcript} in full. Do not read or modify anything else. Every piece of evidence must be a verbatim quote from the transcript. Return the structured result only.`,
  { label: `critic-${i}`, phase: 'Critique', model: 'opus', effort: 'high', schema: CRITIC })))).filter(Boolean)
log(`${critics.length} critics returned`)

phase('Merge')
const all = critics.flatMap(c => c.weaknesses)
const merged = await agent(
  `Here are ${all.length} weaknesses raised by three independent critics of the same transcript. Merge duplicates (same underlying defect, even if worded differently) into one entry each, keeping the strongest evidence quote and the most concrete fix; keep distinct defects separate. Do not invent new ones. Input:\n${JSON.stringify(all, null, 1)}`,
  { label: 'merge', phase: 'Merge', model: 'opus', schema: MERGED })
const weaknesses = merged ? merged.weaknesses : all
log(`${weaknesses.length} distinct weaknesses to verify`)

phase('Verify')
const verified = await pipeline(weaknesses,
  w => parallel([
    () => agent(`Evidence lens. A critic claims this weakness about a Lantern transcript:\n${JSON.stringify(w, null, 1)}\nRead ${transcript}. Is the quoted evidence actually there, verbatim or near-verbatim, and does it support the claim when read in context (including the HELD-OUT EVENTS line and the RIG LINT section)? Default to real=false if the quote is missing or the reading is unfair. Set cause to 'not-real' when refuting, otherwise 'harness'. Return the verdict only.`,
      { label: `evidence:${w.title.slice(0, 40)}`, phase: 'Verify', model: 'opus', schema: VERDICT }),
    () => agent(`Cause lens. A critic claims this weakness about a Lantern transcript and proposes a fix:\n${JSON.stringify(w, null, 1)}\nDecide where the defect lives. Read what you need, read-only: the transcript ${transcript}; Lantern's code under ${plugin} (hooks.py builds the context every session gets, insights.py computes the watches and the since-last block, jobs.py and discovery.py run between sessions, lantern.py is the CLI the model calls); the rig under ${rig} (week.py is the scenario, session.py is the scripted user and permission policy). cause='harness' if Lantern's code or instructions produce it and the fix belongs there (give file_hint as path:line); 'rig' if the simulated user, permission policy or scenario produced it; 'model' if Lantern gave the model what it needed and the model ignored it; 'not-real' if the claim does not hold. real=true only for 'harness' or 'model'. Return the verdict only.`,
      { label: `cause:${w.title.slice(0, 40)}`, phase: 'Verify', model: 'opus', effort: 'high', schema: VERDICT }),
  ]).then(([e, c]) => ({ ...w, evidence_ok: !!(e && e.real), cause: c ? c.cause : 'unknown', cause_note: c ? c.note : '', file_hint: c && c.file_hint || '', evidence_note: e ? e.note : '' })))
const results = verified.filter(Boolean)
const confirmed = results.filter(r => r.evidence_ok && (r.cause === 'harness' || r.cause === 'model')).sort((a, b) => b.impact - a.impact)
const rig_issues = results.filter(r => r.cause === 'rig')
const rejected = results.filter(r => !confirmed.includes(r) && !rig_issues.includes(r))
log(`${confirmed.length} confirmed, ${rig_issues.length} rig issues, ${rejected.length} rejected`)

phase('Synthesize')
const summary = await agent(
  `Write a short, plain-English ranked fix list (markdown, under 350 words) for the Lantern build team from these confirmed weaknesses, most impact first, each with its file hint and a one-line fix. Then one line on what the rig itself should change, from these rig issues. No preamble.\nConfirmed:\n${JSON.stringify(confirmed, null, 1)}\nRig issues:\n${JSON.stringify(rig_issues, null, 1)}`,
  { label: 'synthesize', phase: 'Synthesize', model: 'opus' })

const median = k => { const v = critics.map(c => c.scores[k]).sort((a, b) => a - b); return v.length ? v[Math.floor(v.length / 2)] : null }
return {
  medians: { watches: median('watches'), open_loops: median('open_loops'), rules: median('rules'), since_last: median('since_last'), sharper: median('sharper') },
  per_critic: critics.map(c => c.scores),
  engineer: critics.map(c => c.engineer), non_technical: critics.map(c => c.non_technical),
  confirmed, rig_issues, rejected, summary,
}