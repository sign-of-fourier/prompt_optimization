import React, { useState } from 'react'
import { DOCS_URL } from './brand.js'

// In-app hints. Deliberately few: a hint belongs only where the user makes a decision they can get wrong.
// Naming a control is a title= tooltip, not a hint. The budget is eight; adding a ninth means removing one.
//
// The body must not contain anything the linked page does not - the page is the source, the hint is a preview.
// `tests/test_hints.py` asserts every `doc` below resolves to a real anchor in impromptune/docs, so the two
// cannot drift apart silently.
export const HINTS = {
  evaluate: {
    title: 'Evaluate decides what "better" means',
    body: 'A scorer turns each answer into a number by comparing one output field with the row\'s label. The objective then weighs those numbers against token counts, so a negative weight on template_tokens makes the search pay for length. Nothing is collapsed inside a scorer.',
    doc: '/docs/evaluate',
  },
  optimizer: {
    title: 'The loop is fixed; these are its knobs',
    body: 'Each round shows a reflection model a few of the current best prompt\'s failures, asks for a rewrite of one prompt, and keeps it only if it beats its parent on the same small batch. Rounds, spend and patience all stop the run - whichever comes first.',
    doc: '/docs/optimization',
  },
  data: {
    title: 'The hold-out is the number that counts',
    body: 'A fraction of your rows is set aside before the search starts and never shown to it. The best score seen during a run is the maximum of noisy evaluations and reads high; the hold-out does not. Run the pilot first - it prices the run and measures how far two identical evaluations disagree.',
    doc: '/docs/data#holdout',
  },
  step: {
    title: 'A step is never rewritten',
    body: 'It calls a service once per row, before any prompt, and its answers become placeholders. Authorise it with a pasted key, or with an OAuth connection where we have an app registered. Training reads a frozen snapshot of its answers; production calls it live, on purpose.',
    doc: '/docs/steps#auth',
  },
  keys: {
    title: 'Six secrets, six jobs',
    body: 'An endpoint is your own model account; a step key authorises an external step; a connection is an OAuth grant to act on your account elsewhere; an API key is what a caller sends us to reach a served version. Yours are encrypted and revocable here; API keys are stored only as a hash.',
    doc: '/docs/keys',
  },
  versions: {
    title: 'A version is immutable',
    body: 'Publishing snapshots the prompts, models and steps of the run that produced them - not whatever your canvas says now - along with the score it earned. Its fingerprint covers behaviour only, so moving a node does not make a new one. To change anything, publish another.',
    doc: '/docs/serving#versions',
  },
  traces: {
    title: 'Traces become tomorrow\'s dataset',
    body: 'Every served request records its inputs, the version that answered, and what each prompt and step produced. Add an outcome - what actually happened - and the pair becomes a label you can promote into a dataset. Mix in uncorrected traces, or you will train on failures alone.',
    doc: '/docs/serving#outcomes',
  },
  library: {
    title: 'A template becomes your project',
    body: 'Each entry is a program, its settings and a sample dataset, ready to run. Adding one copies it into your projects; what you change afterwards is yours alone and affects no one else.',
    doc: '/docs/quickstart#templates',
  },
}

export default function Hint({ id }) {
  const h = HINTS[id]
  const [open, setOpen] = useState(false)
  if (!h) return null
  return (
    <span className="hintw">
      <button type="button" className="hinti" aria-label={'About: ' + h.title} title="What is this?"
              onClick={e => { e.stopPropagation(); setOpen(!open) }}>i</button>
      {open && <span className="hintp" onClick={e => e.stopPropagation()}>
        <b>{h.title}</b>
        <span>{h.body}</span>
        <a href={DOCS_URL + h.doc.replace(/^\/docs/, '')} target="_blank" rel="noopener">Read more &rarr;</a>
        <button type="button" className="small" onClick={() => setOpen(false)}>close</button>
      </span>}
    </span>
  )
}
