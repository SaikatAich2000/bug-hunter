/** Pure rules the work-item dialog applies before saving; each returns the message to show, or null when fine. */

/** Types that are not planned into sprints one by one (Sub-tasks follow their parent, Epics span sprints). */
export const UNPLANNED_TYPES = ["Epic", "Sub-task"];

const plural = (n, one, many) => (n > 1 ? many : one);

/** The first missing required field, as a message. */
export function payloadProblem(payload) {
  if (!payload.project_id) return "Please pick a project";
  if (!payload.title) return "Title is required";
  if (!payload.reporter_id) return "Reporter is required";
  return null;
}

/** Files staged on an existing item (as attachments or on an unposted comment) that Save would silently drop. */
export function unsentFilesProblem(attachmentCount, commentFileCount) {
  if (attachmentCount > 0) {
    const n = attachmentCount;
    return (
      `You have ${n} attachment${plural(n, "", "s")} that ${plural(n, "hasn't", "haven't")} been uploaded yet. `
      + 'Click "Upload" in the Attachments section, or remove '
      + `${plural(n, "it", "them")}, before saving`
    );
  }
  if (commentFileCount > 0) {
    const n = commentFileCount;
    return (
      `You have ${n} file${plural(n, "", "s")} attached to a comment you haven't posted yet. `
      + 'Click "Post" to add the comment, or remove '
      + `${plural(n, "it", "them")}, before saving`
    );
  }
  return null;
}

/** Warning when the due date falls outside the picked sprint's date range, else null. */
export function dueOutsideSprintMessage(sprint, due) {
  if (!sprint || !due) return null;
  const before = sprint.start_date && due < sprint.start_date;
  const after = sprint.end_date && due > sprint.end_date;
  if (!before && !after) return null;
  return (
    `This item's due date (${due}) falls outside "${sprint.name}"'s sprint window `
    + `(${sprint.start_date || "?"} – ${sprint.end_date || "?"}). Add it to this sprint anyway?`
  );
}

/** Why an Agile-only type cannot be created while the project's Agile surface is off. */
export function agileOffMessage(typeLabel, canManage) {
  const how = canManage ? " Use “Enable Agile” above" : " Ask a manager or admin to enable it";
  return `Agile is not enabled for this project, so ${typeLabel} items cannot be created yet.${how}`;
}
