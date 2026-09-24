import { useState, useEffect, useCallback } from 'react'
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from 'react/jsx-runtime'

const APP = 'meeting-transcribe'
const ACCENT = 'var(--accent, #7c3aed)'
const ACCENT_BG = 'var(--accent-subtle, #e8d5f5)'

// ---------------------------------------------------------------- bootstrap
// config.json is written by the app's self-heal cron. Until it lands we show an
// initializing state rather than a cryptic error.
let STATE_PATH = ''
let APP_JSON_PATH = ''
let CONFIG_MISSING = false

const configReady = fetch(`/api/apps/${APP}/config`)
  .then(r => (r.ok ? r.json() : null))
  .then(cfg => {
    if (cfg && cfg.statePath) {
      STATE_PATH = cfg.statePath
      APP_JSON_PATH = cfg.appJsonPath || ''
    } else {
      CONFIG_MISSING = true
    }
  })
  .catch(() => { CONFIG_MISSING = true })

function readFile (path) {
  if (!path) return Promise.resolve(null)
  return fetch('/api/file-read?path=' + encodeURIComponent(path))
    .then(r => (r.ok ? r.text() : null))
    .catch(() => null)
}

function runInBackground (message, slot) {
  return fetch('/api/chat?ws=1', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, slot })
  }).catch(() => {})
}

// ---------------------------------------------------------------- primitives

const STAGES = {
  'refused': { label: 'REFUSED', bg: 'var(--danger-subtle, #fee2e2)', fg: 'var(--danger, #b91c1c)' },
  'failed': { label: 'FAILED', bg: 'var(--danger-subtle, #fee2e2)', fg: 'var(--danger, #b91c1c)' },
  'preflight-ok': { label: 'CLEARED', bg: '#fef3c7', fg: '#b45309' },
  'extracted': { label: 'EXTRACTED', bg: '#fef3c7', fg: '#b45309' },
  'running': { label: 'RUNNING', bg: ACCENT_BG, fg: ACCENT },
  'transcribed': { label: 'TRANSCRIBED', bg: ACCENT_BG, fg: ACCENT },
  'rendered': { label: 'DONE', bg: '#d1fae5', fg: 'var(--ok, #047857)' },
  'completed-not-downloaded': { label: 'NEEDS FETCH', bg: '#fef3c7', fg: '#b45309' }
}

function Badge ({ stage }) {
  const s = STAGES[stage] || { label: (stage || '?').toUpperCase(), bg: ACCENT_BG, fg: ACCENT }
  return _jsx('span', {
    style: {
      background: s.bg, color: s.fg, padding: '2px 7px', borderRadius: '9999px',
      fontSize: '10px', fontWeight: 600, letterSpacing: '0.02em', whiteSpace: 'nowrap'
    },
    children: s.label
  })
}

function Btn ({ onClick, disabled, children, primary, title }) {
  return _jsx('button', {
    onClick, disabled, title,
    style: {
      background: primary ? (disabled ? 'var(--muted)' : ACCENT) : 'transparent',
      color: primary ? 'var(--accent-fg, #fff)' : (disabled ? 'var(--muted)' : ACCENT),
      border: primary ? 'none' : `1px solid ${ACCENT_BG}`,
      padding: '5px 14px', borderRadius: '9999px', fontSize: '11px', fontWeight: 500,
      cursor: disabled ? 'default' : 'pointer', whiteSpace: 'nowrap'
    },
    children
  })
}

function Card ({ children, tone }) {
  return _jsx('div', {
    style: {
      background: 'var(--card, var(--bg))',
      border: `1px solid ${tone === 'danger' ? 'var(--danger, #b91c1c)' : 'var(--border)'}`,
      borderRadius: '6px', padding: '14px', marginBottom: '12px'
    },
    children
  })
}

function SectionTitle ({ children }) {
  return _jsx('div', {
    style: { fontSize: '13px', fontWeight: 600, color: ACCENT, marginBottom: '8px' },
    children
  })
}

function hms (sec) {
  if (!sec) return '-'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h ? `${h}h${String(m).padStart(2, '0')}m` : `${m}m${String(s % 60).padStart(2, '0')}s`
}

function baseName (p) {
  if (!p) return '-'
  const parts = String(p).split(/[\\/]/)
  return parts[parts.length - 1]
}

// ---------------------------------------------------------------- panels

function RefusalEvidence ({ job }) {
  const zp = job.zeros_proof || {}
  const zerosProven = zp.max_db != null && zp.max_db < -80
  return _jsxs('div', {
    style: { fontSize: '11px', color: 'var(--text)', lineHeight: 1.6 },
    children: [
      _jsxs('div', {
        children: [
          'This file is ',
          _jsxs('b', { children: [job.silence_pct, '% silence'] }),
          ` (${Math.round(job.silence_sec)}s of ${Math.round(job.duration_sec)}s). `,
          `Submitting would bill the full ${(job.duration_sec / 60).toFixed(1)} minutes `,
          `(~$${(job.est_cost_usd || 0).toFixed(2)}) and return almost nothing.`
        ]
      }),
      _jsxs('div', {
        style: { marginTop: '6px', color: 'var(--muted)' },
        children: [`Whole file: mean ${job.mean_db} dB, max ${job.max_db} dB.`]
      }),
      zp.window_from_min != null && _jsxs('div', {
        style: { marginTop: '6px' },
        children: [
          `Quietest window ${zp.window_from_min}-${zp.window_from_min + 5} min measured `,
          `${zp.window_mean_db} dB; after +40 dB amplification it reads mean `,
          `${zp.mean_db} dB, max ${zp.max_db} dB. `,
          zerosProven
            ? _jsx('b', { children: 'The samples are true zeros - nothing is recoverable there.' })
            : 'Some signal survives amplification, so it may be salvageable.'
        ]
      }),
      _jsx('div', {
        style: { marginTop: '8px', fontWeight: 600 },
        children: 'This is a capture-side failure, not a transcription problem. Check the recorder audio source and run a two-minute test capture before the next long meeting.'
      }),
      _jsx(VolumeMap, { blocks: job.blocks || [] })
    ]
  })
}

function VolumeMap ({ blocks }) {
  if (!blocks.length) return null
  const vals = blocks.map(b => (b.mean_db == null ? -91 : b.mean_db))
  const lo = Math.min(...vals)
  const hi = Math.max(...vals)
  const span = hi - lo || 1
  return _jsxs('div', {
    style: { marginTop: '10px' },
    children: [
      _jsx('div', {
        style: { fontSize: '10px', color: 'var(--muted)', marginBottom: '4px' },
        children: 'mean volume per 5 min (taller = louder)'
      }),
      _jsx('div', {
        style: { display: 'flex', alignItems: 'flex-end', gap: '2px', height: '34px' },
        children: blocks.map((b, i) => {
          const v = b.mean_db == null ? -91 : b.mean_db
          const frac = (v - lo) / span
          const dead = frac < 0.05
          return _jsx('div', {
            title: `${b.from_min}-${b.from_min + 5} min: ${b.mean_db} dB`,
            style: {
              flex: 1, minWidth: '3px',
              height: `${Math.max(2, frac * 34)}px`,
              background: dead ? 'var(--danger, #b91c1c)' : ACCENT,
              opacity: dead ? 0.45 : 1, borderRadius: '1px'
            }
          }, i)
        })
      })
    ]
  })
}

function JobRow ({ job, onView }) {
  const [open, setOpen] = useState(false)
  const refused = job.stage === 'refused'
  const canView = !!job.transcript_md

  return _jsxs('div', {
    style: { borderBottom: '1px solid var(--border)', padding: '8px 0' },
    children: [
      _jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', flexWrap: 'wrap' },
        children: [
          _jsx('span', {
            style: {
              maxWidth: '240px', overflow: 'hidden', textOverflow: 'ellipsis',
              whiteSpace: 'nowrap', fontWeight: 500
            },
            title: job.source,
            children: baseName(job.source)
          }),
          _jsx(Badge, { stage: job.stage }),
          _jsx('span', { style: { color: 'var(--muted)', fontSize: '11px' }, children: hms(job.duration_sec) }),
          job.silence_pct != null && _jsxs('span', {
            style: { color: job.silence_pct >= 80 ? 'var(--danger, #b91c1c)' : 'var(--muted)', fontSize: '11px' },
            children: [job.silence_pct, '% silent']
          }),
          job.est_cost_usd != null && _jsxs('span', {
            style: { color: 'var(--muted)', fontSize: '11px' },
            children: ['~$', (job.est_cost_usd).toFixed(2)]
          }),
          job.words != null && _jsxs('span', {
            style: { color: 'var(--muted)', fontSize: '11px' },
            children: [job.words, ' words']
          }),
          _jsxs('span', {
            style: { marginLeft: 'auto', display: 'flex', gap: '6px' },
            children: [
              refused && _jsx(Btn, {
                onClick: () => setOpen(!open),
                children: open ? 'Hide evidence' : 'Why refused'
              }),
              canView && _jsx(Btn, { onClick: () => onView(job), children: 'Transcript' })
            ]
          })
        ]
      }),
      open && refused && _jsx('div', {
        style: { marginTop: '8px', paddingLeft: '2px' },
        children: _jsx(RefusalEvidence, { job })
      })
    ]
  })
}

function TranscriptViewer ({ job, text, onClose }) {
  return _jsxs('div', {
    style: {
      position: 'absolute', inset: 0, background: 'var(--bg)', zIndex: 5,
      display: 'flex', flexDirection: 'column', padding: '16px', overflow: 'hidden'
    },
    children: [
      _jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' },
        children: [
          _jsx('span', { style: { fontSize: '13px', fontWeight: 600 }, children: baseName(job.source) }),
          _jsx(Badge, { stage: job.stage }),
          _jsx('span', { style: { marginLeft: 'auto' }, children: _jsx(Btn, { onClick: onClose, children: 'Close' }) })
        ]
      }),
      _jsx('pre', {
        style: {
          flex: 1, overflow: 'auto', margin: 0, whiteSpace: 'pre-wrap',
          wordBreak: 'break-word', fontSize: '12px', lineHeight: 1.6,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          color: 'var(--text)'
        },
        children: text == null ? 'Loading...' : text
      })
    ]
  })
}

// ---------------------------------------------------------------- page

export default function MeetingTranscribe () {
  const [state, setState] = useState(null)
  const [version, setVersion] = useState('')
  const [ready, setReady] = useState(false)
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState('')
  const [viewing, setViewing] = useState(null)
  const [viewText, setViewText] = useState(null)

  const load = useCallback(async () => {
    await configReady
    if (!STATE_PATH) { setReady(true); return }
    const txt = await readFile(STATE_PATH)
    if (txt) {
      try { setState(JSON.parse(txt)) } catch { /* mid-write; next tick */ }
    } else {
      setState({ jobs: [] })
    }
    setReady(true)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  useEffect(() => {
    configReady.then(() => {
      if (!APP_JSON_PATH) return
      readFile(APP_JSON_PATH).then(t => {
        if (!t) return
        try { setVersion(JSON.parse(t).version || '') } catch { /* ignore */ }
      })
    })
  }, [])

  const openTranscript = useCallback(async job => {
    setViewing(job)
    setViewText(null)
    setViewText(await readFile(job.transcript_md) ?? 'Could not read the transcript file.')
  }, [])

  const startPreflight = useCallback(async () => {
    const p = path.trim().replace(/^"|"$/g, '')
    if (!p) return
    setBusy('preflight')
    await runInBackground(
      `Use the transcribe-pipeline skill on this recording: ${p}\n\n` +
      'Run probe then preflight FIRST. If preflight refuses, stop and report the ' +
      'measured evidence - do not pass --force and do not submit. If it clears, ' +
      'continue through extract, submit, poll and render, then write minutes only ' +
      'if the transcript contains an actual meeting.',
      `${APP}-run`
    )
    setPath('')
    setTimeout(() => { setBusy(''); load() }, 6000)
  }, [path, load])

  const refresh = useCallback(async () => {
    setBusy('refresh')
    await runInBackground(
      `Poll in-flight meeting-transcribe jobs: run the pipeline script's poll verb, ` +
      'then render any job that reached the transcribed stage. Stay silent if ' +
      'nothing is in flight.',
      `${APP}-poll`
    )
    setTimeout(() => { setBusy(''); load() }, 6000)
  }, [load])

  const jobs = (state && state.jobs) || []
  const running = jobs.filter(j => j.stage === 'running').length
  const spent = jobs
    .filter(j => j.submitted_at)
    .reduce((a, j) => a + (j.est_cost_usd || 0), 0)
  const saved = jobs
    .filter(j => j.stage === 'refused')
    .reduce((a, j) => a + (j.est_cost_usd || 0), 0)

  return _jsxs('div', {
    style: {
      position: 'relative', maxWidth: '1200px', margin: '0 auto', padding: '16px',
      color: 'var(--text)', fontSize: '12px'
    },
    children: [
      // header
      _jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', gap: '10px', flexWrap: 'wrap' },
        children: [
          _jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: '10px' },
            children: [
              _jsxs('svg', {
                xmlns: 'http://www.w3.org/2000/svg', width: 20, height: 20,
                viewBox: '0 0 24 24', fill: 'none', stroke: ACCENT, strokeWidth: 2,
                strokeLinecap: 'round', strokeLinejoin: 'round',
                children: [
                  _jsx('path', { d: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z' }),
                  _jsx('path', { d: 'M19 10v2a7 7 0 0 1-14 0v-2' }),
                  _jsx('line', { x1: '12', y1: '19', x2: '12', y2: '22' })
                ]
              }),
              _jsx('h2', { style: { margin: 0, fontSize: '18px' }, children: 'Meeting Transcribe' }),
              running > 0 && _jsx(Badge, { stage: 'running' })
            ]
          }),
          _jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: '10px' },
            children: [
              _jsx('span', {
                style: { fontSize: '11px', color: 'var(--muted)' },
                children: `${jobs.length} job${jobs.length === 1 ? '' : 's'}`
              }),
              _jsx(Btn, {
                onClick: refresh, disabled: busy === 'refresh',
                children: busy === 'refresh' ? 'Checking...' : 'Refresh'
              }),
              _jsx('span', {
                style: { fontSize: '10px', color: 'var(--muted)' },
                children: version ? `v${version}` : ''
              })
            ]
          })
        ]
      }),

      CONFIG_MISSING && _jsx(Card, {
        children: _jsxs('div', {
          style: { fontSize: '11px', lineHeight: 1.6 },
          children: [
            _jsx('b', { children: 'Initializing.' }),
            ' The app\'s maintenance cron writes its config on first run (within 5 minutes). ',
            'Nothing is wrong yet - reload this page after it fires.'
          ]
        })
      }),

      // submit card
      _jsxs(Card, {
        children: [
          _jsx(SectionTitle, { children: 'Transcribe a recording' }),
          _jsxs('div', {
            style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' },
            children: [
              _jsx('input', {
                value: path,
                onChange: e => setPath(e.target.value),
                onKeyDown: e => { if (e.key === 'Enter') startPreflight() },
                placeholder: 'Full path to a video or audio file',
                style: {
                  flex: 1, minWidth: '260px', padding: '6px 10px', fontSize: '12px',
                  background: 'var(--bg)', color: 'var(--text)',
                  border: '1px solid var(--border)', borderRadius: '6px'
                }
              }),
              _jsx(Btn, {
                primary: true, onClick: startPreflight,
                disabled: !path.trim() || busy === 'preflight',
                children: busy === 'preflight' ? 'Starting...' : 'Measure first (free)'
              })
            ]
          }),
          _jsx('div', {
            style: { marginTop: '8px', fontSize: '11px', color: 'var(--muted)', lineHeight: 1.5 },
            children: 'Nothing billable runs until the silence and cost check passes. A file that is more than 80% silence is refused with the measurements that justify it.'
          })
        ]
      }),

      (spent > 0 || saved > 0) && _jsx(Card, {
        children: _jsxs('div', {
          style: { display: 'flex', gap: '24px', fontSize: '11px', flexWrap: 'wrap' },
          children: [
            _jsxs('span', { children: [_jsx('span', { style: { color: 'var(--muted)' }, children: 'Submitted: ' }), `~$${spent.toFixed(2)}`] }),
            saved > 0 && _jsxs('span', {
              children: [
                _jsx('span', { style: { color: 'var(--muted)' }, children: 'Refused before billing: ' }),
                _jsxs('b', { style: { color: 'var(--ok, #047857)' }, children: ['~$', saved.toFixed(2)] })
              ]
            })
          ]
        })
      }),

      // queue
      _jsxs(Card, {
        children: [
          _jsx(SectionTitle, { children: `Jobs (${jobs.length})` }),
          !ready
            ? _jsx('div', { style: { color: 'var(--muted)', fontSize: '11px' }, children: 'Loading...' })
            : jobs.length === 0
              ? _jsx('div', {
                  style: { color: 'var(--muted)', fontSize: '11px', padding: '8px 0' },
                  children: 'No jobs yet. Paste a file path above to measure one.'
                })
              : _jsx('div', {
                  children: jobs.map(j => _jsx(JobRow, { job: j, onView: openTranscript }, j.id))
                })
        ]
      }),

      viewing && _jsx(TranscriptViewer, {
        job: viewing, text: viewText,
        onClose: () => { setViewing(null); setViewText(null) }
      })
    ]
  })
}
