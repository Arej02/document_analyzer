// The plan as a document: eight collapsible sections, the at-a-glance rail and an outline (tasks_frontend.md
// A2.2 to A2.4). The outline is drawn into the app sidebar's slot (`OutlineSlot`) below the repository picker; with
// no slot it falls back to its own column. It reads only; every write control is a slot supplied by PlanPage, which still owns the
// existing behaviour (approval, overrides, submit, the unit panel):
//
//   slots.approval                 "by you at …" on an approved plan, shown in the rail
//   slots.unitDetail               the existing `PlanUnitDetail` panel, shown when a unit is selected
//   slots.renderControls(card)     approval controls, merge bar and override errors under a phase card
//   slots.renderUnitExtra(u,i,card) merge tick and move buttons beside a unit row
//   slots.renderAcknowledge(card)  the facade acknowledge form, or undefined when the plan cannot take one
import { useMemo } from 'react'
import { createPortal } from 'react-dom'
import { Copy } from 'lucide-react'
import { useOutlineSlot } from '../OutlineSlot.jsx'
import { Toast } from '../ui/Toast.jsx'
import { useCopy } from '../ui/useCopy.js'
import Architecture, { ARCHITECTURE_SUBS } from './Architecture.jsx'
import DataLayer from './DataLayer.jsx'
import DependencyInventory from './DependencyInventory.jsx'
import ExecutiveSummary from './ExecutiveSummary.jsx'
import MigrationPlan from './MigrationPlan.jsx'
import OpenQuestions from './OpenQuestions.jsx'
import PlanDocument from './PlanDocument.jsx'
import Rail from './Rail.jsx'
import RepositoryOverview, { OVERVIEW_SUBS } from './RepositoryOverview.jsx'
import RiskAssessment from './RiskAssessment.jsx'
import Section from './Section.jsx'
import Toc from './Toc.jsx'
import { buildModuleRows } from './architectureModel.js'
import { useCollapsed } from './collapse.js'
import { buildExecutiveSummary, buildRepoOverview } from './overviewModel.js'
import { migrationSubs } from './migrationModel.js'
import { openQuestions } from './openQuestionsModel.js'
import { reviewProgress } from './railModel.js'
import { riskItems } from './riskModel.js'
import { toMarkdown } from './toMarkdown.js'
import { useOutline } from './useOutline.js'
import { useRepoOverview } from './useRepoOverview.js'
import { useRiskDetails } from './useRiskDetails.js'
import { useSchemaEntities } from './useSchemaEntities.js'
import { useUnitGraph } from './useUnitGraph.js'
import './agent2.css'

// `intro(actions)` (the page title and status) heads the document column, so the rail starts level with it,
// and places `actions` (Copy as Markdown) in its top row;
// `outro` (next steps) closes it, so it spans the same width.
export default function PlanDocumentView({ plan, planId, selectedUnitId, onSelectUnit, slots, intro = null, outro = null }) {
  const repo = useRepoOverview(plan.source_job_id)
  const schema = useSchemaEntities(plan.source_job_id)
  const unitGraph = useUnitGraph(planId, 'unit')
  const phaseGraph = useUnitGraph(planId, 'phase')
  const risks = useMemo(() => riskItems(plan), [plan])
  const riskDetails = useRiskDetails(planId, risks)
  const [copyMessage, copy] = useCopy()

  const sections = useMemo(() => [
    { id: 'summary', title: 'Executive summary', subs: [] },
    { id: 'overview', title: 'Repository overview', subs: OVERVIEW_SUBS },
    { id: 'architecture', title: 'Architecture and module map', subs: ARCHITECTURE_SUBS },
    { id: 'dependencies', title: 'Dependency inventory', subs: [] },
    { id: 'data', title: 'Data layer and schema', subs: [] },
    { id: 'risks', title: 'Risk assessment', subs: [] },
    { id: 'plan', title: 'Migration plan', subs: migrationSubs(plan) },
    { id: 'questions', title: 'Open questions and decisions', subs: [] },
  ], [plan])
  const sectionIds = useMemo(() => sections.map((s) => s.id), [sections])
  const collapse = useCollapsed(sectionIds)
  const outline = useOutline(sections, { beforeNavigate: collapse.open })
  const slot = useOutlineSlot()
  // On narrow screens the sidebar is a drawer: close it once a section is chosen.
  const navigate = (id) => { outline.navigate(id); slot.close?.() }

  const executive = useMemo(() => buildExecutiveSummary(plan, repo.summary), [plan, repo.summary])
  const overview = useMemo(() => buildRepoOverview({ stats: repo.stats, summary: repo.summary, routes: repo.routes }), [repo.stats, repo.summary, repo.routes])
  const moduleRows = useMemo(() => buildModuleRows(plan, unitGraph.graph), [plan, unitGraph.graph])
  const questions = openQuestions(plan)
  const review = reviewProgress(plan.phases)

  const meta = {
    risks: `${risks.length} at medium or high`,
    plan: `${review.approved} of ${review.total} phases approved`,
    questions: questions.openFlags ? `${questions.openFlags} open` : 'None open',
  }
  const extras = { risks: String(risks.length), plan: `${review.approved}/${review.total}`, questions: questions.openFlags ? `${questions.openFlags} open` : 'Done' }

  const bodies = {
    summary: <ExecutiveSummary model={executive} />,
    overview: (
      <RepositoryOverview
        model={overview}
        loading={{ summary: repo.summary == null && !repo.failed.summary, routes: repo.routes == null && !repo.failed.routes }}
        failed={repo.failed}
      />
    ),
    architecture: (
      <Architecture
        rows={moduleRows}
        graphError={unitGraph.error}
        onSelectUnit={onSelectUnit}
        systemMap={{
          plan,
          phaseGraph: phaseGraph.graph,
          graphError: phaseGraph.error,
          routes: repo.routes,
          schemaTotal: schema.data ? (schema.data.total_count ?? schema.data.entities?.length ?? null) : null,
          packageCount: Number.isFinite(repo.stats.dependencies) ? repo.stats.dependencies : null,
        }}
      />
    ),
    dependencies: <DependencyInventory rows={null} declaredCount={Number.isFinite(repo.stats.dependencies) ? repo.stats.dependencies : null} />,
    data: <DataLayer state={schema} />,
    risks: <RiskAssessment plan={plan} details={riskDetails.byId} detailedIds={riskDetails.detailedIds} onSelectUnit={onSelectUnit} />,
    plan: (
      <MigrationPlan
        plan={plan}
        planId={planId}
        phaseGraph={phaseGraph.graph}
        selectedUnitId={selectedUnitId}
        onSelectUnit={onSelectUnit}
        renderControls={slots.renderControls}
        renderUnitExtra={slots.renderUnitExtra}
      />
    ),
    questions: <OpenQuestions plan={plan} onSelectUnit={onSelectUnit} renderAcknowledge={slots.renderAcknowledge} />,
  }

  const exportMarkdown = () => copy(toMarkdown({
    plan,
    repoSummary: repo.summary,
    stats: repo.stats,
    routes: repo.routes,
    unitGraph: unitGraph.graph,
    phaseGraph: phaseGraph.graph,
    dependencies: null,
    schema: schema.data,
    riskDetails: riskDetails.byId,
  }), 'Plan copied as Markdown')

  const toc = (
    <Toc
      variant={slot.el ? 'sidebar' : 'inline'}
      sections={sections}
      activeId={outline.activeId}
      activeSection={outline.activeSection}
      expanded={outline.expanded}
      onToggle={outline.toggle}
      onNavigate={navigate}
      seen={outline.seen}
      extras={extras}
      open={outline.tocOpen}
      onOpen={outline.toggleTocOpen}
    />
  )

  return (
    <>
      {slot.el ? createPortal(toc, slot.el) : null}
      <PlanDocument
        outline={slot.el ? null : toc}
        rail={<Rail plan={plan} approval={slots.approval} />}
        glance={<Rail plan={plan} approval={slots.approval} />}
        header={typeof intro === 'function'
          ? intro(<button type="button" className="a2-chip-btn" onClick={exportMarkdown}><Copy size={14} className="ui-icon" aria-hidden="true" /> Copy as Markdown</button>)
          : intro}
      >
        {selectedUnitId ? slots.unitDetail : null}
        {sections.map((s, i) => (
          <Section key={s.id} id={s.id} index={i + 1} title={s.title} meta={meta[s.id]} collapsed={collapse.collapsed.has(s.id)} onToggle={collapse.toggle}>
            {bodies[s.id]}
          </Section>
        ))}
        {outro}
      </PlanDocument>
      <Toast message={copyMessage} />
    </>
  )
}
