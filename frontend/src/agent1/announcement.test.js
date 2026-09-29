import test from 'node:test'
import assert from 'node:assert/strict'
import { buildRunView } from './buildRunView.js'
import { runAnnouncement } from './announcement.js'

const running = (progress_stage) => buildRunView({ job: { status: 'running', progress_stage } })

test('announces the phase, the run outcome and the queued state', () => {
  assert.equal(runAnnouncement(buildRunView({ job: { status: 'queued' } })), 'Analysis queued.')
  assert.equal(runAnnouncement(running('summarizing')), 'Step 2 of 3: Summarizing.')
  assert.equal(runAnnouncement(buildRunView({ job: { status: 'done' } })), 'Analysis complete.')
  assert.equal(runAnnouncement(buildRunView({ job: { status: 'failed', progress_stage: 'extracting' } })), 'Analysis failed during Extracting.')
  assert.equal(runAnnouncement(buildRunView({ job: { status: 'failed' } })), 'Analysis failed.')
  assert.equal(runAnnouncement(buildRunView({ job: { status: 'cancelled', progress_stage: 'summarizing' } })), 'Analysis cancelled.')
})

test('the text does not change with elapsed time, so ticks are not announced', () => {
  const at = (now) => runAnnouncement(buildRunView({ job: { status: 'running', progress_stage: 'extracting' }, now }))
  assert.equal(at(1000), at(999000))
})
