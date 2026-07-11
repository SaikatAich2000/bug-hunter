/** Global app state: user, meta, directory, bug list + filters, view, shared modals. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import type {
  BugDetail,
  BugListResponse,
  BugOut,
  Filters,
  HealthOut,
  ItemType,
  MeOut,
  MetaOut,
  NotificationOut,
  ProjectOut,
  StatsOut,
  UnreadCountOut,
  UserOut,
  ViewName,
} from "../types";

export type TabName = "all" | ItemType;

/** KPI → status-filter mapping. */
export const KPI_FILTER_MAP: Record<string, string[]> = {
  total: [],
  open: ["New", "In Progress", "Reopened"],
  resolved: ["Resolved"],
  closed: ["Closed"],
  resolve_later: ["Resolve Later"],
};

export const EMPTY_FILTERS: Filters = {
  project_id: [],
  status: [],
  priority: [],
  environment: [],
  assignee_id: [],
  item_type: [],
  reporter_id: null,
  q: "",
};

const FALLBACK_META: MetaOut = {
  statuses: ["New", "In Progress", "Resolved", "Closed", "Reopened", "Not a Bug", "Resolve Later"],
  statuses_by_type: {
    Bug: ["New", "In Progress", "Resolved", "Closed", "Reopened", "Not a Bug", "Resolve Later"],
    Requirement: ["New", "In Progress", "Resolved", "Closed"],
    Task: ["New", "In Progress", "Resolved", "Closed"],
  },
  priorities: ["Low", "Medium", "High", "Critical"],
  environments: ["DEV", "UAT", "PROD"],
  item_types: ["Bug", "Requirement", "Task"],
};

export interface BugModalState {
  open: boolean;
  /** Loaded detail when viewing/editing; null = create mode. */
  bug: BugDetail | null;
  /** Create-mode defaults (split-button type pick, "+ Add Task" in events). */
  defaultType?: ItemType;
  defaultEventId?: number | null;
}

export interface ProjectModalState {
  open: boolean;
  project: ProjectOut | null;
}

export interface UserModalState {
  open: boolean;
  user: UserOut | null;
}

export interface AppState {
  currentUser: MeOut;
  meta: MetaOut;
  users: UserOut[];
  projects: ProjectOut[];
  stats: StatsOut | null;
  health: HealthOut | null;

  view: ViewName;
  setView: (v: ViewName) => void;

  activeTab: TabName;
  setActiveTab: (t: TabName) => void;
  defaultNewType: ItemType;
  setDefaultNewType: (t: ItemType) => void;

  bugs: BugOut[];
  page: number;
  setPage: (p: number) => void;
  pageSize: number;
  total: number;
  totalPages: number;

  filters: Filters;
  setFilters: (f: Filters | ((prev: Filters) => Filters)) => void;
  clearFilters: () => void;

  sidebarCollapsed: boolean;
  toggleSidebarCollapsed: () => void;

  // Data refreshers
  refreshBugs: () => Promise<void>;
  refreshStats: () => Promise<void>;
  refreshAll: () => Promise<void>;
  loadUsers: () => Promise<void>;
  loadProjects: () => Promise<void>;

  // Shared modals
  bugModal: BugModalState;
  openBugForm: (opts?: { defaultType?: ItemType; defaultEventId?: number | null }) => void;
  openBugDetail: (bugId: number) => Promise<void>;
  reloadBugModal: () => Promise<void>;
  closeBugModal: () => void;

  projectModal: ProjectModalState;
  openProjectForm: (project?: ProjectOut | null) => void;
  closeProjectModal: () => void;

  userModal: UserModalState;
  openUserForm: (user?: UserOut | null) => void;
  closeUserModal: () => void;

  changePasswordOpen: boolean;
  setChangePasswordOpen: (open: boolean) => void;

  // Per-user notifications
  notifications: NotificationOut[];
  unreadCount: number;
  loadNotifications: () => Promise<void>;
  markNotificationRead: (id: number) => Promise<void>;
  markAllNotificationsRead: () => Promise<void>;
  deleteNotification: (id: number) => Promise<void>;
  /** Mark read + navigate to the linked bug/event. */
  openNotification: (n: NotificationOut) => Promise<void>;

  /** Role rank helper (admin 3 > manager 2 > user 1). */
  roleRank: (role: string) => number;
  canManage: boolean; // manager or admin
  isAdmin: boolean;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside provider");
  return v;
}

const PAGE_SIZE = 20;
const SESSION_POLL_MS = 15_000;
/** Shared poll interval for list, stats, directory, open modal, and per-view pollers. */
export const DATA_POLL_MS = 10_000;
const VERSION_POLL_MS = 5 * 60_000;

function readLs(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

/** Shallow MeOut compare — unchanged session polls keep object identity (no re-render). */
function sameMe(a: MeOut, b: MeOut): boolean {
  return (
    a.id === b.id &&
    a.name === b.name &&
    a.email === b.email &&
    a.role === b.role &&
    a.is_active === b.is_active
  );
}

export function AppProvider({
  me,
  children,
}: {
  me: MeOut;
  children: ReactNode;
}) {
  const [currentUser, setCurrentUser] = useState<MeOut>(me);
  const [meta, setMeta] = useState<MetaOut>(FALLBACK_META);
  const [users, setUsers] = useState<UserOut[]>([]);
  const [projects, setProjects] = useState<ProjectOut[]>([]);
  const [stats, setStats] = useState<StatsOut | null>(null);
  const [health, setHealth] = useState<HealthOut | null>(null);

  const [view, setViewState] = useState<ViewName>("list");
  const [activeTab, setActiveTabState] = useState<TabName>(() => {
    const t = readLs("activeTab", "all");
    return (["all", "Bug", "Requirement", "Task"] as const).includes(t as TabName)
      ? (t as TabName)
      : "all";
  });
  const [defaultNewType, setDefaultNewTypeState] = useState<ItemType>(() => {
    const t = readLs("defaultNewType", "Bug");
    return (["Bug", "Requirement", "Task"] as const).includes(t as ItemType)
      ? (t as ItemType)
      : "Bug";
  });

  const [bugs, setBugs] = useState<BugOut[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [filters, setFiltersState] = useState<Filters>(EMPTY_FILTERS);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => readLs("sidebarCollapsed", "0") === "1",
  );

  const [bugModal, setBugModal] = useState<BugModalState>({ open: false, bug: null });
  const [projectModal, setProjectModal] = useState<ProjectModalState>({ open: false, project: null });
  const [userModal, setUserModal] = useState<UserModalState>({ open: false, user: null });
  const [changePasswordOpen, setChangePasswordOpen] = useState(false);

  const [notifications, setNotifications] = useState<NotificationOut[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);

  // Refs let stable callbacks/pollers read latest values without stale closures.
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const notificationsRef = useRef(notifications);
  notificationsRef.current = notifications;
  const pageRef = useRef(page);
  pageRef.current = page;
  const tabRef = useRef(activeTab);
  tabRef.current = activeTab;

  // Poll signatures: skip state commits (and re-renders) on unchanged data.
  const lastBugsSig = useRef("");
  const lastStatsSig = useRef("");
  const lastUsersSig = useRef("");
  const lastProjectsSig = useRef("");

  const buildBugParams = useCallback((): URLSearchParams => {
    const f = filtersRef.current;
    const params = new URLSearchParams();
    params.set("page", String(pageRef.current));
    params.set("page_size", String(PAGE_SIZE));
    for (const id of f.project_id) params.append("project_id", String(id));
    for (const s of f.status) params.append("status", s);
    for (const p of f.priority) params.append("priority", p);
    for (const e of f.environment) params.append("environment", e);
    for (const a of f.assignee_id) params.append("assignee_id", String(a));
    for (const t of f.item_type) params.append("item_type", t);
    if (f.reporter_id != null) params.set("reporter_id", String(f.reporter_id));
    if (f.q.trim()) params.set("q", f.q.trim());
    // Implicit tab filter on top.
    const tab = tabRef.current;
    if (tab !== "all" && !f.item_type.includes(tab)) {
      params.append("item_type", tab);
    }
    return params;
  }, []);

  const refreshBugs = useCallback(async () => {
    try {
      const res = await api<BugListResponse>(`/bugs?${buildBugParams()}`);
      const sig = `${res.total}|${res.pages}|${JSON.stringify(res.items)}`;
      if (sig === lastBugsSig.current) return; // unchanged poll → no re-render
      lastBugsSig.current = sig;
      setBugs(res.items);
      setTotal(res.total);
      setTotalPages(Math.max(1, res.pages));
    } catch (err) {
      toastError(err);
    }
  }, [buildBugParams]);

  const refreshStats = useCallback(async () => {
    try {
      const tab = tabRef.current;
      const params = new URLSearchParams();
      if (tab !== "all") params.set("item_type", tab);
      // Status filter included so Analytics charts react to KPI tile clicks.
      for (const s of filtersRef.current.status) params.append("status", s);
      const qs = params.toString();
      const res = await api<StatsOut>(`/stats${qs ? `?${qs}` : ""}`);
      const sig = JSON.stringify(res);
      if (sig === lastStatsSig.current) return; // unchanged poll → no re-render
      lastStatsSig.current = sig;
      setStats(res);
    } catch (err) {
      toastError(err);
    }
  }, []);

  const refreshUnread = useCallback(async () => {
    try {
      const c = await api<UnreadCountOut>("/notifications/unread_count");
      setUnreadCount(c.unread);
    } catch {
      /* transient — keep the last known count */
    }
  }, []);

  const refreshAll = useCallback(async () => {
    // Unread badge rides along so mutation sites get a fresh count immediately.
    await Promise.all([refreshBugs(), refreshStats(), refreshUnread()]);
  }, [refreshBugs, refreshStats, refreshUnread]);

  const loadUsers = useCallback(async () => {
    try {
      const res = await api<UserOut[]>("/users");
      const sig = JSON.stringify(res);
      if (sig === lastUsersSig.current) return; // unchanged → no re-render
      lastUsersSig.current = sig;
      setUsers(res);
    } catch (err) {
      toastError(err);
    }
  }, []);

  const loadProjects = useCallback(async () => {
    try {
      const res = await api<ProjectOut[]>("/projects");
      const sig = JSON.stringify(res);
      if (sig === lastProjectsSig.current) return; // unchanged → no re-render
      lastProjectsSig.current = sig;
      setProjects(res);
    } catch (err) {
      toastError(err);
    }
  }, []);

  // Monotonic token: discards out-of-order poll responses; bumped by optimistic mutations.
  const notifSeqRef = useRef(0);
  const loadNotifications = useCallback(async () => {
    const seq = ++notifSeqRef.current;
    try {
      const [list, count] = await Promise.all([
        api<NotificationOut[]>("/notifications?limit=50"),
        api<UnreadCountOut>("/notifications/unread_count"),
      ]);
      if (seq !== notifSeqRef.current) return; // superseded
      setNotifications(list);
      setUnreadCount(count.unread);
    } catch (err) {
      toastError(err);
    }
  }, []);

  // Boot (auth gate lives in main.tsx).
  const bootedRef = useRef(false);
  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    (async () => {
      try {
        const [h, m] = await Promise.all([
          api<HealthOut>("/health"),
          api<MetaOut>("/meta"),
          loadUsers(),
          loadProjects(),
        ]);
        setHealth(h);
        if (m && Array.isArray(m.statuses)) setMeta({ ...FALLBACK_META, ...m });
      } catch (err) {
        toastError(err);
      }
      void loadNotifications();
      await refreshAll();
    })();
  }, [loadUsers, loadProjects, refreshAll, loadNotifications]);

  // Re-fetch the list on page / filter / tab change.
  const firstListEffect = useRef(true);
  useEffect(() => {
    if (firstListEffect.current) {
      // boot() already triggers the initial refreshAll
      firstListEffect.current = false;
      return;
    }
    void refreshBugs();
  }, [page, filters, activeTab, refreshBugs]);

  // Tab change additionally refreshes stats.
  const firstTabEffect = useRef(true);
  useEffect(() => {
    if (firstTabEffect.current) {
      firstTabEffect.current = false;
      return;
    }
    void refreshStats();
  }, [activeTab, refreshStats]);

  useEffect(() => {
    // Session poll every 15s + on tab focus; unread count rides the same tick.
    const tick = async () => {
      try {
        const m = await api<MeOut>("/auth/me", { cache: "no-store" });
        setCurrentUser((prev) => (sameMe(prev, m) ? prev : m));
      } catch {
        /* 401 already bounced; network errors don't kick the user out */
      }
      try {
        const c = await api<UnreadCountOut>("/notifications/unread_count");
        setUnreadCount(c.unread);
      } catch {
        /* transient — keep the last known count */
      }
    };
    const id = setInterval(tick, SESSION_POLL_MS);
    const onVis = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  // Live ref so the interval calls the latest refreshAll without re-registering.
  const refreshAllRef = useRef(refreshAll);
  refreshAllRef.current = refreshAll;

  useEffect(() => {
    // Poll list + stats; paused while hidden, fires on refocus.
    const refresh = () => {
      if (!document.hidden) void refreshAllRef.current();
    };
    const id = setInterval(refresh, DATA_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  // Directory polls separately so bug saves don't double-fetch users/projects.
  const loadDirRef = useRef({ loadUsers, loadProjects });
  loadDirRef.current = { loadUsers, loadProjects };
  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      void loadDirRef.current.loadUsers();
      void loadDirRef.current.loadProjects();
    };
    const id = setInterval(refresh, DATA_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  useEffect(() => {
    // Notify once when the deployed asset version changes (new release).
    let warned = false;
    const boot = health?.asset_version;
    if (!boot) return;
    const id = setInterval(async () => {
      try {
        const h = await api<HealthOut>("/health");
        if (!warned && h.asset_version && h.asset_version !== boot) {
          warned = true;
          toast("A new version of Bug Hunter is available — refresh to update", "info");
        }
      } catch {
        /* ignore */
      }
    }, VERSION_POLL_MS);
    return () => clearInterval(id);
  }, [health?.asset_version]);

  const setActiveTab = useCallback((t: TabName) => {
    setActiveTabState(t);
    setPage(1);
    try {
      localStorage.setItem("activeTab", t);
    } catch {
      /* private mode */
    }
  }, []);

  const setDefaultNewType = useCallback((t: ItemType) => {
    setDefaultNewTypeState(t);
    try {
      localStorage.setItem("defaultNewType", t);
    } catch {
      /* private mode */
    }
  }, []);

  const setFilters = useCallback(
    (f: Filters | ((prev: Filters) => Filters)) => {
      setFiltersState((prev) => {
        const next = typeof f === "function" ? f(prev) : f;
        return next;
      });
      setPage(1);
    },
    [],
  );

  const clearFilters = useCallback(() => {
    setFiltersState(EMPTY_FILTERS);
    setPage(1);
  }, []);

  const toggleSidebarCollapsed = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("sidebarCollapsed", next ? "1" : "0");
      } catch {
        /* private mode */
      }
      return next;
    });
  }, []);

  // Reflect collapse state on <body> (styles.css keys off body class).
  useEffect(() => {
    document.body.classList.toggle("sidebar-collapsed", sidebarCollapsed);
  }, [sidebarCollapsed]);

  const setView = useCallback((v: ViewName) => {
    setViewState(v);
  }, []);

  const openBugForm = useCallback(
    (opts?: { defaultType?: ItemType; defaultEventId?: number | null }) => {
      setBugModal({
        open: true,
        bug: null,
        defaultType: opts?.defaultType,
        defaultEventId: opts?.defaultEventId ?? null,
      });
    },
    [],
  );

  const openBugDetail = useCallback(async (bugId: number) => {
    try {
      const bug = await api<BugDetail>(`/bugs/${bugId}`);
      setBugModal({ open: true, bug });
    } catch (err) {
      toastError(err);
    }
  }, []);

  const reloadBugModal = useCallback(async () => {
    const id = bugModal.bug?.id;
    if (!id) return;
    try {
      const bug = await api<BugDetail>(`/bugs/${id}`);
      setBugModal((prev) => ({ ...prev, bug }));
    } catch (err) {
      toastError(err);
    }
  }, [bugModal.bug?.id]);

  const closeBugModal = useCallback(() => {
    setBugModal({ open: false, bug: null });
  }, []);

  // Poll the open bug modal so concurrent edits appear; keyed on bug id.
  const reloadBugModalRef = useRef(reloadBugModal);
  reloadBugModalRef.current = reloadBugModal;
  const bugModalOpenId = bugModal.open && bugModal.bug ? bugModal.bug.id : null;
  useEffect(() => {
    if (bugModalOpenId == null) return;
    const tick = () => {
      if (!document.hidden) void reloadBugModalRef.current();
    };
    const id = setInterval(tick, DATA_POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [bugModalOpenId]);

  const openProjectForm = useCallback((project?: ProjectOut | null) => {
    setProjectModal({ open: true, project: project ?? null });
  }, []);
  const closeProjectModal = useCallback(() => {
    setProjectModal({ open: false, project: null });
  }, []);

  const openUserForm = useCallback((user?: UserOut | null) => {
    setUserModal({ open: true, user: user ?? null });
  }, []);
  const closeUserModal = useCallback(() => {
    setUserModal({ open: false, user: null });
  }, []);

  const markNotificationRead = useCallback(async (id: number) => {
    // Optimistic: stamp read locally, then persist.
    notifSeqRef.current++; // in-flight poll can't un-read this
    setNotifications((prev) =>
      prev.map((n) =>
        n.id === id && !n.read_at ? { ...n, read_at: new Date().toISOString() } : n,
      ),
    );
    setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await api(`/notifications/${id}/read`, { method: "POST" });
    } catch (err) {
      toastError(err);
      void loadNotifications();
    }
  }, [loadNotifications]);

  const markAllNotificationsRead = useCallback(async () => {
    const now = new Date().toISOString();
    notifSeqRef.current++;
    setNotifications((prev) => prev.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    setUnreadCount(0);
    try {
      await api("/notifications/read-all", { method: "POST" });
    } catch (err) {
      toastError(err);
      void loadNotifications();
    }
  }, [loadNotifications]);

  const deleteNotification = useCallback(async (id: number) => {
    notifSeqRef.current++;
    // Read unread status outside the updater so the count adjusts exactly once.
    const gone = notificationsRef.current.find((n) => n.id === id);
    const wasUnread = !!gone && !gone.read_at;
    setNotifications((prev) => prev.filter((n) => n.id !== id));
    if (wasUnread) setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await api(`/notifications/${id}`, { method: "DELETE" });
    } catch (err) {
      toastError(err);
      void loadNotifications();
    }
  }, [loadNotifications]);

  const openNotification = useCallback(
    async (n: NotificationOut) => {
      if (!n.read_at) void markNotificationRead(n.id);
      if (n.bug_id != null) {
        await openBugDetail(n.bug_id);
      } else if (n.event_id != null) {
        setView("events");
      }
    },
    [markNotificationRead, openBugDetail, setView],
  );

  // Sleuth chat "open bug" deep-link.
  useEffect(() => {
    const onOpen = (e: Event) => {
      const id = (e as CustomEvent<{ bugId?: number }>).detail?.bugId;
      if (typeof id === "number") void openBugDetail(id);
    };
    document.addEventListener("sleuth:open-bug", onOpen);
    return () => document.removeEventListener("sleuth:open-bug", onOpen);
  }, [openBugDetail]);

  const roleRank = useCallback((role: string): number => {
    if (role === "admin") return 3;
    if (role === "manager") return 2;
    return 1;
  }, []);

  const value = useMemo<AppState>(
    () => ({
      currentUser,
      meta,
      users,
      projects,
      stats,
      health,
      view,
      setView,
      activeTab,
      setActiveTab,
      defaultNewType,
      setDefaultNewType,
      bugs,
      page,
      setPage,
      pageSize: PAGE_SIZE,
      total,
      totalPages,
      filters,
      setFilters,
      clearFilters,
      sidebarCollapsed,
      toggleSidebarCollapsed,
      refreshBugs,
      refreshStats,
      refreshAll,
      loadUsers,
      loadProjects,
      bugModal,
      openBugForm,
      openBugDetail,
      reloadBugModal,
      closeBugModal,
      projectModal,
      openProjectForm,
      closeProjectModal,
      userModal,
      openUserForm,
      closeUserModal,
      changePasswordOpen,
      setChangePasswordOpen,
      notifications,
      unreadCount,
      loadNotifications,
      markNotificationRead,
      markAllNotificationsRead,
      deleteNotification,
      openNotification,
      roleRank,
      canManage: roleRank(currentUser.role) >= 2,
      isAdmin: currentUser.role === "admin",
    }),
    [
      currentUser, meta, users, projects, stats, health, view, setView,
      activeTab, setActiveTab, defaultNewType, setDefaultNewType, bugs, page,
      total, totalPages, filters, setFilters, clearFilters, sidebarCollapsed,
      toggleSidebarCollapsed, refreshBugs, refreshStats, refreshAll, loadUsers,
      loadProjects, bugModal, openBugForm, openBugDetail, reloadBugModal,
      closeBugModal, projectModal, openProjectForm, closeProjectModal,
      userModal, openUserForm, closeUserModal, changePasswordOpen, roleRank,
      notifications, unreadCount, loadNotifications, markNotificationRead,
      markAllNotificationsRead, deleteNotification, openNotification,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
