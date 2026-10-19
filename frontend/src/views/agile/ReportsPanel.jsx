/** Reports: burndown, burnup, sprint report, velocity,
 * cumulative flow, control chart, epic report, daily summary, workload. */
import Skeleton from "../../components/Skeleton";
import { useEffect, useMemo, useState } from "react";
import { useApp } from "../../state/AppContext";
import BhSelect from "../../components/BhSelect";
import BhDateInput from "../../components/BhDateInput";
import { apiBlob } from "../../lib/api";
import { toastError } from "../../lib/toast";
import { agileApi } from "./agileApi";
import { useLatestLoad } from "./useAgileProject";
import { IssueKey, StatusLozenge, TypeIcon } from "./IssueBits";
import { formatDateRange, localIsoDate, localToday } from "./agileShared";
import { CycleChart, FlowChart, SprintChart, VelocityChart } from "./charts/Charts";
import { formatNumber, statisticLabel } from "./logic/estimates";

const REPORTS = [
  { key: "burndown", label: "Burndown chart", scope: "sprint", help: "Work left in the sprint against the ideal guideline." },
  { key: "burnup", label: "Burnup chart", scope: "sprint", help: "Work completed against the sprint's total scope." },
  { key: "sprint", label: "Sprint report", scope: "sprint", help: "What was committed, completed, carried over and removed." },
  { key: "velocity", label: "Velocity chart", scope: "board", help: "Commitment and completed work of recent sprints." },
  { key: "cfd", label: "Cumulative flow diagram", scope: "range", help: "Issues in each board column, day by day." },
  { key: "control", label: "Control chart", scope: "range", help: "Cycle time of completed issues." },
  { key: "epic", label: "Epic report", scope: "epic", help: "An epic's progress and the work each sprint completed." },
  { key: "daily", label: "Daily summary", scope: "sprint", help: "Where every sprint issue stood at the end of a day." },
  { key: "workload", label: "Workload", scope: "sprint", help: "Assigned work per person against their capacity." },
];

function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return localIsoDate(d);
}

async function download(path, fallbackName, method = "GET") {
  try {
    const { blob, filename } = await apiBlob(path, { method });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    toastError(err);
  }
}

function IssueTable({ title, rows, mode, onOpen, showStart }) {
  return (
    <section className="ag-report-section">
      <h4>{title} <span className="muted small">({rows.length})</span></h4>
      {rows.length === 0 ? <p className="muted small">None.</p> : (
        <table className="reports-table ag-table">
          <thead>
            <tr><th>Key</th><th>Summary</th><th>Type</th><th>Status</th>
              {mode !== "item_count" && <th className="num">{showStart ? "Estimate (start → end)" : "Estimate"}</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><IssueKey issue={r} onOpen={onOpen} />{r.added_during_sprint && <span title="Added after the sprint started"> *</span>}</td>
                <td>{r.title}</td>
                <td><TypeIcon type={r.item_type} /> {r.item_type}</td>
                <td><StatusLozenge status={r.status} category={r.status_category} /></td>
                {mode !== "item_count" && (
                  <td className="num">
                    {showStart && r.estimate_at_start != null && r.estimate_at_start !== r.estimate
                      ? `${formatNumber(r.estimate_at_start)} → ${formatNumber(r.estimate)}` : formatNumber(r.estimate)}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function SprintReport({ report, onOpen }) {
  const mode = report.estimation_mode;
  const rate = report.committed_estimate ? Math.round((report.completed_estimate / report.committed_estimate) * 100) : 0;
  return (
    <div>
      <div className="report-kpis">
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.committed_estimate)}</span><span>Committed at start</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.completed_estimate)}</span><span>Completed</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.incomplete_estimate)}</span><span>Not completed</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.added_estimate)}</span><span>Added after start</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.removed_estimate)}</span><span>Removed</span></div>
        {mode !== "item_count" && (
          <div className="report-kpi"><span className="report-kpi-num">{rate}%</span><span>Completed vs committed</span></div>
        )}
      </div>
      {!report.history_complete && (
        <p className="ag-notice">This sprint started before full change history was recorded; its figures come from the sprint&apos;s start and completion records.</p>
      )}
      <IssueTable title="Completed issues" rows={report.completed} mode={mode} onOpen={onOpen} showStart />
      <IssueTable title="Issues not completed" rows={report.not_completed} mode={mode} onOpen={onOpen} showStart />
      <IssueTable title="Issues removed from sprint" rows={report.removed} mode={mode} onOpen={onOpen} />
      {report.completed_outside.length > 0 && (
        <IssueTable title="Issues completed outside of this sprint" rows={report.completed_outside} mode={mode} onOpen={onOpen} />
      )}
      <p className="muted small">* Added to the sprint after it started. Estimates in {statisticLabel(mode).toLowerCase()}.</p>
    </div>
  );
}

function BurnTable({ report }) {
  if (!report.events.length) return <p className="muted small">No changes since the sprint started.</p>;
  return (
    <table className="reports-table ag-table">
      <caption className="muted small">Changes during the sprint</caption>
      <thead><tr><th>When</th><th>Issue</th><th>Event</th><th className="num">Change</th><th className="num">Remaining</th></tr></thead>
      <tbody>
        {report.events.map((e) => (
          <tr key={`${e.at}-${e.work_item_id}-${e.event}`}>
            <td>{new Date(e.at).toLocaleString()}</td>
            <td>{e.display_id} {e.title}</td>
            <td>{e.event}{e.scope_change && <span className="ag-scope-tag"> scope change</span>}</td>
            <td className="num">{e.change > 0 ? "+" : ""}{formatNumber(e.change)}</td>
            <td className="num">{formatNumber(e.remaining)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EpicReport({ report, onOpen }) {
  const mode = report.estimation_mode;
  const p = report.progress;
  return (
    <div>
      <div className="report-kpis">
        <div className="report-kpi"><span className="report-kpi-num">{p.completed_child_count}/{p.child_count}</span><span>Issues done</span></div>
        {mode !== "item_count" && <div className="report-kpi"><span className="report-kpi-num">{formatNumber(p.completed_estimate)}/{formatNumber(p.total_estimate)}</span><span>Estimate done</span></div>}
        <div className="report-kpi"><span className="report-kpi-num">{p.unestimated_count}</span><span>Unestimated</span></div>
      </div>
      <section className="ag-report-section">
        <h4>By sprint</h4>
        {report.sprints.length === 0 ? <p className="muted small">No sprint has worked on this epic yet.</p> : (
          <table className="reports-table ag-table">
            <thead><tr><th>Sprint</th><th className="num">Completed in sprint</th><th className="num">Epic scope at sprint end</th><th className="num">Remaining at sprint end</th></tr></thead>
            <tbody>
              {report.sprints.map((s) => (
                <tr key={s.sprint_id}>
                  <td>{s.sprint_name}{s.state === "active" && " (active)"}</td>
                  <td className="num">{formatNumber(s.completed_estimate)} ({s.completed_count})</td>
                  <td className="num">{formatNumber(s.scope_estimate)}</td>
                  <td className="num">{formatNumber(s.remaining_estimate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <IssueTable title="Done" rows={report.done} mode={mode} onOpen={onOpen} />
      <IssueTable title="In progress" rows={report.in_progress} mode={mode} onOpen={onOpen} />
      <IssueTable title="To do" rows={report.todo} mode={mode} onOpen={onOpen} />
    </div>
  );
}

function DailyReport({ report }) {
  return (
    <div>
      <div className="report-kpis">
        <div className="report-kpi"><span className="report-kpi-num">{report.todo_count}</span><span>To do</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.in_progress_count}</span><span>In progress</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.testing_count}</span><span>Testing</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.done_count}</span><span>Done</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.blocked_count}</span><span>Blocked or flagged</span></div>
      </div>
      <p className="report-points">
        <span>Completed: <strong>{formatNumber(report.completed_estimate)}</strong></span>
        <span>Remaining: <strong>{formatNumber(report.remaining_estimate)}</strong></span>
        <span>Completed this day: <strong>{formatNumber(report.completed_estimate_today)}</strong></span>
        <span>Added this day: <strong>{report.scope_added_today}</strong></span>
        <span>Removed this day: <strong>{report.scope_removed_today}</strong></span>
      </p>
      <h4>Issues at the end of the day</h4>
      <table className="reports-table ag-table">
        <thead><tr><th>Key</th><th>Summary</th><th>Status</th><th>Column</th><th className="num">Estimate</th></tr></thead>
        <tbody>
          {report.items.map((i) => (
            <tr key={i.work_item_id}><td>{i.display_id}</td><td>{i.title}</td><td>{i.status}</td><td>{i.column}</td><td className="num">{formatNumber(i.story_points)}</td></tr>
          ))}
        </tbody>
      </table>
      <h4>What moved that day</h4>
      {report.transitions.length === 0 ? <p className="muted small">Nothing changed.</p> : (
        <table className="reports-table ag-table">
          <thead><tr><th>When</th><th>Issue</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody>
            {report.transitions.map((t) => (
              <tr key={`${t.occurred_at}-${t.work_item_id}-${t.event_type}`}>
                <td>{new Date(t.occurred_at).toLocaleTimeString()}</td><td>{t.title}</td><td>{t.event_type.replace("_", " ")}</td><td>{t.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {report.blockers.length > 0 && (
        <>
          <h4>Blocked or flagged now</h4>
          <ul>{report.blockers.map((b) => <li key={b.work_item_id}>{b.title} — {b.blocked_reason}</li>)}</ul>
        </>
      )}
    </div>
  );
}

const EXPORT_KEYS = {
  burndown: "burndown", burnup: "burnup", sprint: "sprint", velocity: "velocity",
  cfd: "cumulative-flow", control: "control-chart", epic: "epic", workload: "workload",
};

/** Keeps `current` pointing at an item of `list`: the first one when it is missing or no longer listed. */
function useAutoSelect(list, current, setCurrent) {
  useEffect(() => {
    if (!list.length) setCurrent(null);
    else if (!list.some((x) => x.id === current)) setCurrent(list[0].id);
  }, [list, current, setCurrent]);
}

/** Fetches the chosen report, or null while its inputs are incomplete. */
function loadReport({ meta, reportKey, sprintId, epicId, range, rangeValid, day, boardId }) {
  const missing = { sprint: !sprintId, epic: !epicId, range: !rangeValid }[meta.scope];
  if (missing) return null;
  switch (reportKey) {
    case "burndown":
    case "burnup": return agileApi.burndown(sprintId);
    case "sprint": return agileApi.sprintReport(sprintId);
    case "velocity": return agileApi.velocity(boardId, 7);
    case "cfd": return agileApi.cumulativeFlow(boardId, range.from, range.to);
    case "control": return agileApi.controlChart(boardId, range.from, range.to);
    case "epic": return agileApi.epicReport(epicId);
    case "daily": return agileApi.daily(sprintId, day);
    case "workload": return agileApi.workload(sprintId);
    default: return null;
  }
}

function exportQuery({ meta, exportKey, sprintId, epicId, range, boardId }) {
  const q = new URLSearchParams({ report_key: exportKey });
  if (meta.scope === "sprint") q.set("sprint_id", sprintId);
  if (meta.scope === "board" || meta.scope === "range") q.set("board_id", boardId);
  if (meta.scope === "range") { q.set("date_from", range.from); q.set("date_to", range.to); }
  if (meta.scope === "epic") q.set("epic_id", epicId);
  return `/agile/reports/export?${q}`;
}

/** Why there is nothing to chart yet, or null when the report can run. */
function reportNotice({ meta, hasSprints, hasEpics, rangeValid }) {
  if (meta.scope === "sprint" && !hasSprints) return <p className="muted ag-empty">Start a sprint to see this report.</p>;
  if (meta.scope === "epic" && !hasEpics) return <p className="muted ag-empty">Create an epic to see this report.</p>;
  if (meta.scope === "range" && !rangeValid) return <p className="ag-error">Pick a start date on or before the end date.</p>;
  return null;
}

function KpiStrip({ items }) {
  return (
    <div className="report-kpis">
      {items.map(([value, label]) => (
        <div className="report-kpi" key={label}><span className="report-kpi-num">{value}</span><span>{label}</span></div>
      ))}
    </div>
  );
}

function BurnReport({ data, kind, unitLabel }) {
  return (
    <>
      <KpiStrip items={[
        [formatNumber(data.committed_estimate), "Committed"],
        [formatNumber(data.remaining_estimate), "Remaining"],
        [formatNumber(data.completed_estimate), "Completed"],
        [formatNumber(data.scope_estimate), "Total scope"],
      ]} />
      <SprintChart report={data} kind={kind} unitLabel={unitLabel} />
      <BurnTable report={data} />
    </>
  );
}

function VelocityReport({ data, unitLabel }) {
  return (
    <>
      <VelocityChart report={data} unitLabel={unitLabel} />
      {data.points.length > 0 && (
        <table className="reports-table ag-table">
          <thead><tr><th>Sprint</th><th>Dates</th><th className="num">Commitment</th><th className="num">Completed</th></tr></thead>
          <tbody>{data.points.map((p) => (
            <tr key={p.sprint_id}><td>{p.sprint_name}</td><td>{formatDateRange(p.start_date, p.end_date)}</td>
              <td className="num">{formatNumber(p.committed_estimate)}</td><td className="num">{formatNumber(p.completed_estimate)}</td></tr>
          ))}</tbody>
        </table>
      )}
    </>
  );
}

function ControlReport({ data }) {
  return (
    <>
      <KpiStrip items={[
        [data.count, "Issues"],
        [data.average_days, "Average (days)"],
        [data.median_days, "Median (days)"],
        [`${data.min_days}–${data.max_days}`, "Min–max (days)"],
      ]} />
      <p className="muted small">Cycle time = time spent in {data.work_columns.join(", ") || "the working columns"}.</p>
      <CycleChart report={data} />
    </>
  );
}

function WorkloadReport({ data }) {
  if (data.entries.length === 0 && !data.unassigned_count) return <p className="muted ag-empty">Nobody has work in this sprint yet.</p>;
  return (
    <table className="reports-table ag-table">
      <thead><tr><th>Person</th><th className="num">Issues</th><th className="num">Assigned</th><th className="num">Capacity</th><th className="num">Load</th></tr></thead>
      <tbody>
        {data.entries.map((e) => (
          <tr key={e.user_id} className={e.utilization_pct > 100 ? "ag-over" : ""}>
            <td>{e.user_name}</td><td className="num">{e.assigned_count}</td><td className="num">{formatNumber(e.assigned_estimate)}</td>
            <td className="num">{e.capacity_value != null ? formatNumber(e.capacity_value) : "–"}</td>
            <td className="num">{e.utilization_pct != null ? `${e.utilization_pct}%` : "–"}</td>
          </tr>
        ))}
        {data.unassigned_count > 0 && (
          <tr><td><em>Unassigned</em></td><td className="num">{data.unassigned_count}</td><td className="num">{formatNumber(data.unassigned_estimate)}</td><td /><td /></tr>
        )}
      </tbody>
    </table>
  );
}

function ReportBody({ reportKey, data, unitLabel, onOpen }) {
  switch (reportKey) {
    case "burndown":
    case "burnup": return <BurnReport data={data} kind={reportKey} unitLabel={unitLabel} />;
    case "sprint": return <SprintReport report={data} onOpen={onOpen} />;
    case "velocity": return <VelocityReport data={data} unitLabel={unitLabel} />;
    case "cfd": return <FlowChart report={data} />;
    case "control": return <ControlReport data={data} />;
    case "epic": return <EpicReport report={data} onOpen={onOpen} />;
    case "daily": return <DailyReport report={data} />;
    case "workload": return <WorkloadReport data={data} />;
    default: return null;
  }
}

/** The scope-specific pickers in the report header: sprint, epic, date range, day. */
function ReportPickers({ meta, reportKey, reportable, sprintId, setSprintId, epicList, epicId, setEpicId, range, setRange, day, setDay }) {
  return (
    <>
      {meta.scope === "sprint" && reportable.length > 0 && (
        <BhSelect ariaLabel="Sprint" value={sprintId ? String(sprintId) : ""} onChange={(v) => setSprintId(Number(v))}
          options={reportable.map((s) => ({ value: String(s.id), label: `${s.name}${s.state === "active" ? " (active)" : ""}` }))} />
      )}
      {meta.scope === "epic" && epicList.length > 0 && (
        <BhSelect ariaLabel="Epic" value={epicId ? String(epicId) : ""} onChange={(v) => setEpicId(Number(v))}
          options={epicList.map((e) => ({ value: String(e.id), label: `${e.display_id} ${e.title}` }))} />
      )}
      {meta.scope === "range" && (
        <>
          <label className="muted small" htmlFor="ag-range-from">From</label>
          <BhDateInput id="ag-range-from" value={range.from} onChange={(v) => setRange((r) => ({ ...r, from: v }))} />
          <label className="muted small" htmlFor="ag-range-to">To</label>
          <BhDateInput id="ag-range-to" value={range.to} onChange={(v) => setRange((r) => ({ ...r, to: v }))} />
        </>
      )}
      {reportKey === "daily" && (
        <>
          <label className="muted small" htmlFor="ag-daily-date">Day</label>
          <BhDateInput id="ag-daily-date" value={day} onChange={setDay} />
        </>
      )}
    </>
  );
}

export default function ReportsPanel({ projectId, board }) {
  const { openBugDetail } = useApp();
  const mode = board.estimation_mode;
  const [reportKey, setReportKey] = useState("burndown");
  const [sprintId, setSprintId] = useState(null);
  const [epicId, setEpicId] = useState(null);
  const [range, setRange] = useState(() => ({ from: daysAgo(30), to: localToday() }));
  const [day, setDay] = useState(localToday);

  const sprints = useLatestLoad(() => agileApi.sprints(board.id), [board.id]);
  const epics = useLatestLoad(() => agileApi.epics(projectId), [projectId]);
  const epicList = epics.data || [];
  const reportable = useMemo(() => (sprints.data || [])
    .filter((s) => s.state === "active" || s.state === "closed")
    .sort((a, b) => b.sequence_number - a.sequence_number), [sprints.data]);
  useAutoSelect(reportable, sprintId, setSprintId);
  useAutoSelect(epicList, epicId, setEpicId);

  const meta = REPORTS.find((r) => r.key === reportKey);
  const rangeValid = Boolean(range.from && range.to && range.from <= range.to);
  const inputs = { meta, reportKey, sprintId, epicId, range, rangeValid, day, boardId: board.id };
  const result = useLatestLoad(async () => loadReport(inputs),
    [reportKey, sprintId, epicId, range.from, range.to, day, board.id]);

  const sprint = reportable.find((s) => s.id === sprintId);
  const unitLabel = statisticLabel(mode);
  const exportKey = EXPORT_KEYS[reportKey];
  const data = result.data;
  let body = reportNotice({ meta, hasSprints: reportable.length > 0, hasEpics: epicList.length > 0, rangeValid });
  if (!body && data) body = <ReportBody reportKey={reportKey} data={data} unitLabel={unitLabel} onOpen={openBugDetail} />;
  else if (!body && result.loading) body = <Skeleton lines={4} className="sprints-loading" />;

  return (
    <div className="ag-reports">
      <nav className="ag-report-nav" aria-label="Reports">
        {REPORTS.map((r) => (
          <button key={r.key} type="button" className={`ag-report-link${r.key === reportKey ? " active" : ""}`}
            aria-current={r.key === reportKey ? "page" : undefined} onClick={() => setReportKey(r.key)}>
            <span className="ag-report-name">{r.label}</span>
            <span className="muted small">{r.help}</span>
          </button>
        ))}
      </nav>
      <section className="ag-report-body" aria-labelledby="ag-report-title">
        <header className="ag-report-head">
          <h3 id="ag-report-title">{meta.label}</h3>
          <ReportPickers {...{ meta, reportKey, reportable, sprintId, setSprintId, epicList, epicId, setEpicId, range, setRange, day, setDay }} />
          <span className="ag-spacer" />
          {exportKey && data && (
            <button type="button" className="btn ghost btn-sm" onClick={() => download(exportQuery({ ...inputs, exportKey }), `${exportKey}.csv`, "POST")}>⬇ CSV</button>
          )}
          {reportKey === "sprint" && sprintId && (
            <button type="button" className="btn ghost btn-sm"
              onClick={() => download(`/agile/reports/sprint/${sprintId}/export.xlsx`, `sprint-report-${sprintId}.xlsx`)}>⬇ Excel</button>
          )}
        </header>
        {meta.scope === "sprint" && sprint && (
          <p className="muted small">{sprint.name} · {formatDateRange(sprint.start_date, sprint.end_date)} · {sprint.state}</p>
        )}
        {body}
      </section>
    </div>
  );
}
