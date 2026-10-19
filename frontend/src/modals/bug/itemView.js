/** Pure view-model helpers of the work-item dialog: what the header says, who the reporter is, which options exist. */
import { canEditItemType, itemTypeEmoji } from "./helpers";

/** Create is always editable; edit prefers the server's can_edit, falling back to the role heuristic. */
export function canEditItem(isEdit, bug, currentUser) {
  if (!isEdit) return true;
  return bug.can_edit ?? canEditItemType(currentUser.role, bug.item_type);
}

/** Valid statuses as options; a status the item already holds stays selectable, flagged "(legacy)". */
export function statusOptionsFor(placeholder, validStatuses, status) {
  const options = [placeholder, ...validStatuses.map((s) => ({ value: s, label: s }))];
  if (status && !validStatuses.includes(status)) options.push({ value: status, label: `${status} (legacy)` });
  return options;
}

/** The current event shows by name even before the event list has loaded. */
export function eventOptionsFor(eventOptions, eventId, bug) {
  const missing = eventId && bug?.event_name && !eventOptions.some((o) => o.value === eventId);
  return missing ? [...eventOptions, { value: eventId, label: bug.event_name }] : eventOptions;
}

/** The reporter is fixed: an edited item keeps its original reporter, a new one uses the current user. */
export function reporterFor(isEdit, bug, me) {
  const original = isEdit ? bug.reporter : null;
  const option = original && original.id !== me.id ? original : { id: me.id, name: me.name, email: me.email };
  return { option, id: original ? original.id : me.id };
}

/** Dialog title and subtitle. */
export function headingFor({ isEdit, bug, defaultType, defaultNewType }) {
  if (!isEdit) return { type: defaultType || defaultNewType || "Bug", title: "New Item", subtitle: "" };
  const type = bug.item_type || "Bug";
  return { type, title: `${itemTypeEmoji(type)} ${type} #${bug.id}`, subtitle: bug.title || "" };
}
