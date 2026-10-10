import{w as R,r as d,j as r}from"./rich-editor-1OpxYOLF.js";import{t as E,o as $,j as I}from"./index-r9NYZJsU.js";import"./styles-Mg3Xd_7K.js";import"./branding-C8Bja9NX.js";import"./constants-Cm-Y7Odk.js";const N={"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"},b=n=>n.replace(/[&<>"']/g,e=>N[e]);function _(n){return n.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"")}function P(n){const e=n.trim();return/^(https?:\/\/|mailto:|#)/i.test(e)?e:""}function w(n){const e=[];let a=n.replace(/`([^`]+)`/g,(t,o)=>(e.push(o),`${e.length-1}`));return a=a.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g,"$1<em>$2</em>").replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(t,o,h)=>{const c=P(h.replace(/&amp;/g,"&"));if(!c)return o;const i=/^https?:/i.test(c);return`<a href="${b(c)}"${i?' target="_blank" rel="noopener noreferrer"':""}>${o}</a>`}),a.replace(/\uE000(\d+)\uE000/g,(t,o)=>`<code>${e[Number(o)]}</code>`)}const A=n=>String(n??"").replace(/\uE000/g,"").replace(/\r\n?/g,`
`);function C(n){return n.trim().replace(/^\|/,"").replace(/\|$/,"").split("|").map(e=>e.trim())}const Y=n=>/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(n)&&n.includes("-"),G=n=>/^(#{1,4}\s|>\s?|\s*[-*]\s|\s*\d+\.\s|```|---+\s*$)/.test(n);function O(n){const e=A(n).split(`
`),a=[];let t=0;for(;t<e.length;){const o=e[t];if(!o.trim()){t++;continue}if(o.startsWith("```")){const l=[];for(t++;t<e.length&&!e[t].startsWith("```");)l.push(e[t++]);t++,a.push(`<pre><code>${b(l.join(`
`))}</code></pre>`);continue}const h=/^(#{1,4})\s+(.*)$/.exec(o);if(h){const l=h[1].length;a.push(`<h${l} id="g-${_(h[2])}">${w(b(h[2]))}</h${l}>`),t++;continue}if(/^---+\s*$/.test(o)){a.push("<hr>"),t++;continue}if(o.startsWith(">")){const l=[];for(;t<e.length&&e[t].startsWith(">");)l.push(e[t++].replace(/^>\s?/,""));a.push(`<blockquote>${w(b(l.join(" ")))}</blockquote>`);continue}if(o.includes("|")&&t+1<e.length&&Y(e[t+1])){const l=C(o);t+=2;const p=[];for(;t<e.length&&e[t].includes("|")&&e[t].trim();)p.push(C(e[t++]));const u=(m,s)=>`<${m}>${w(b(s))}</${m}>`;a.push(`<table><thead><tr>${l.map(m=>u("th",m)).join("")}</tr></thead><tbody>`+p.map(m=>`<tr>${m.map(s=>u("td",s)).join("")}</tr>`).join("")+"</tbody></table>");continue}const c=/^\s*([-*]|\d+\.)\s+/.exec(o);if(c){const l=/\d/.test(c[1]),p=[];for(;t<e.length&&/^\s*([-*]|\d+\.)\s+/.test(e[t]);){let u=e[t++].replace(/^\s*([-*]|\d+\.)\s+/,"");for(;t<e.length&&e[t].trim()&&/^\s{2,}\S/.test(e[t])&&!/^\s*([-*]|\d+\.)\s+/.test(e[t]);)u+=` ${e[t++].trim()}`;p.push(`<li>${w(b(u))}</li>`)}a.push(`<${l?"ol":"ul"}>${p.join("")}</${l?"ol":"ul"}>`);continue}const i=[o.trim()];for(t++;t<e.length&&e[t].trim()&&!G(e[t]);)i.push(e[t++].trim());a.push(`<p>${w(b(i.join(" ")))}</p>`)}return D(a.join(`
`))}function D(n){return R.sanitize(n,{ALLOWED_TAGS:["h1","h2","h3","h4","p","ul","ol","li","strong","em","code","pre","a","blockquote","hr","table","thead","tbody","tr","th","td","br"],ALLOWED_ATTR:["href","rel","id"],ADD_ATTR:["target"],ALLOW_DATA_ATTR:!1})}function B(n){const e=[];let a=!1;for(const t of A(n).split(`
`)){t.startsWith("```")&&(a=!a);const o=!a&&/^(#{2,3})\s+(.*)$/.exec(t);o&&e.push({level:o[1].length,text:o[2],id:`g-${_(o[2])}`})}return e}const W=`# Creating and tracking items

Everything you track is an **item**. Open **Work Items** in the top bar to see them all in one table.

## Item types

- **Bug**, **Requirement** and **Task** are the classic types.
- **Epic**, **Story** and **Sub-task** are the agile types. They work with [Sprints](#help/sprints).
- The tab strip above the table filters the list, the KPI tiles and Analytics by type.

All types share one \`#N\` counter, so an item number is unique across the app.

## Create an item

1. Click **+ New Item** in the page header.
2. Give it a title and pick a **Project**. Both are required.
3. Set the **Status**, **Priority**, **Environment** (DEV, UAT or PROD, used by bugs), **Reporter** and one or more **Assignees**.
4. Optionally attach it to an **Event**, and for agile items an **Epic** or **Sprint**.
5. Save. The item appears in the list straight away.

Add comments, attachments and links to other items from the same window once it exists.

## Find and filter

- Type in the search box to match a title, a description or an \`#id\`.
- Click a **KPI tile** to filter by that group; click it again to clear.
- Use the filter bar for project, status, priority, environment and assignee. **Clear** resets everything.

## Update many items at once

Tick the checkboxes at the start of rows. A bulk bar appears with the number selected and lets you change status, priority or environment, or delete. Items you are not allowed to change are skipped by the server.

## Bring items in from a spreadsheet

Managers and admins see **Download Template** and **Bulk Upload** in the header. Download the Excel template, fill in one item per row, then upload the \`.xlsx\` or \`.csv\`. Every row is checked on its own, and you get a count of created and skipped rows with the reason for each skipped one.

## Group items into an event

**Events** collect items for a standup, a review or a release. Managers and admins create one with **+ New Event**, then add existing items or create new ones inside it. Open an event to see all its items together.

## Keep track of changes

Open an item to see its activity: who changed what and when. Notifications for items you are involved in arrive in the bell at the top right.
`,L=`# Sprints, end to end

Sprints follow the usual Scrum workflow. Open **Sprints** in the top bar and choose a project.

## Turn Scrum on

Agile is off for every project until a manager or admin turns it on. Open **Sprints**, pick the project and click **Set up Scrum board**. This creates the board (To Do, In Progress, Testing, Done) and a backlog ranked from the project's open issues. Nothing is deleted or changed. If you are not a manager, ask one to do this.

## The three levels

- **Epic**: a large body of work. Epics are never put into a sprint.
- **Story, Task, Bug and Requirement**: the standard issues. Each can belong to one Epic and is planned into sprints.
- **Sub-task**: always belongs to a standard issue and follows its sprint and Epic.

## Plan

1. Open the **Backlog** tab. Future and active sprints sit above the ranked backlog.
2. Click **Create sprint** and give it a name, a goal and dates.
3. Drag issues to rank them or move them into the sprint. The ⋯ menu on each row does the same without dragging, and the keyboard works too.
4. Check the **Capacity** tab to compare each member's available hours with the committed estimate.

## Run

1. Click **Start sprint**. It needs at least one issue and opens the board.
2. On the **Active sprint** tab, drag a card to another column to change its status. An issue is done when its status is in the right-most column.
3. Use quick filters, swimlanes and flags to focus. Columns can have minimum and maximum limits that warn when the work in progress is too high.

## Finish

Click **Complete sprint**. Finished issues stay with the closed sprint. For each unfinished issue you choose where it goes: the backlog, a future sprint or a new sprint.

## Review

The **Reports** tab has burndown, burnup, sprint report, velocity, cumulative flow, control chart, Epic report, daily summary and workload. Each can be exported to CSV. See [Reports and exports](#help/reports). The **Epics** tab shows each Epic with its issues and progress, and **Board settings** controls columns, limits, the estimation statistic and working days.

## Who can do what

Everyone with access can move cards on the board. Starting, completing and configuring sprints needs a manager or admin. See [Roles and permissions](#help/roles).
`,U=`# Reports and exports

## The Reports page

**Reports** is available to managers and admins. Pick a report type, set the filters and click **Run** to preview it in the page. Then click **Download XLSX** to get the full data as an Excel file.

## Report types

- **Item Detail Export**: one row per item.
- **Resolution Throughput** and **Created vs Resolved Timeline**: how fast work is closed over time.
- **Pending Items Snapshot** and **Aging Report**: what is still open and for how long.
- **Status Distribution**, **Priority Distribution** and **Project Breakdown**: how work is spread out.
- **Time to Resolution**: how long items take from creation to resolved.

Each type starts with a sensible date range. Change **From** and **To**, or clear them for all time. The optional run label is added to the exported file so you can tell exports apart.

The preview shows only the first rows. The downloaded file always has the complete result.

## Analytics

**Analytics** shows charts of status, priority, environment, project and assignee. It follows the type tabs and the KPI filters.

## Sprint reports

Agile reports live inside **Sprints**, on the **Reports** tab: burndown, burnup, sprint report, velocity, cumulative flow, control chart, Epic report, daily summary and workload. They are built from the change log, so a sprint's commitment is what it held when it started. Scope added later is shown separately. Each one can be exported to CSV.

## Ask Sleuth

You can also ask Sleuth, for example "export all bugs in apollo to excel", and it returns a real Excel file. Open it with the **Ask Sleuth** button in the top bar.
`,q=`# Notifications and push

## The bell

The bell at the top right lists what happened on items and events you are involved in. A badge shows how many are unread. Click a notification to jump straight to the item, or use **Mark all read**. The list refreshes by itself while the bell is open.

## Email

Whether email arrives depends on how your administrator set the server up. It can send a message per event, or one **daily digest** that batches everything for the day. If you do not receive any, ask your administrator which email backend is configured.

## Browser push

Push notifications show up even when the app is in a background tab. They are optional and switched on by your administrator.

1. When push is enabled on the server, your browser asks for permission the first time you open the app. Choose **Allow**.
2. That is all. The app registers your browser by itself each time it opens.

If you chose **Block**, change the notification permission for this site in your browser's site settings, then reload. Push needs a secure (https) address, except on localhost. On networks that block Google's push servers it may never arrive, and the bell still works.

Logging out removes your browser's push registration, so a shared computer does not keep receiving your alerts.

## Reduce the noise

Notifications follow your involvement, so being removed as an assignee or reporter stops them for that item. Admins control the email schedule and retention.
`,M=`# Git branches for stories

Bug Hunter can create a feature branch in a GitHub repository for a **User Story**, and remove exactly that branch again. This is optional and off until an administrator sets it up. Pull requests, merges and tags are not part of it.

## One-time setup (admin or manager)

1. Edit the project (click it in the sidebar) and open **Git integration**.
2. Enter the API base URL, the organization and the default base branch.
3. Paste a project token. Use a fine-grained token with Metadata read and Contents read and write. It is stored encrypted and never shown again.
4. Click **Save Git settings**, then **Test Connection**, and **Load repositories** to check the token can see them.

## Create a branch

1. Open a **Story**. The Git branches section is only on Stories; other types are refused.
2. Choose the repository and the base branch.
3. Click **Create Feature Branch**.

The name is generated for you as \`feature_<story number>_<title slug>\`, where the slug comes from the first 25 characters of the title. Asking twice does not create a second branch.

## Remove a branch

Any member of the story's project can remove a branch that Bug Hunter created, if the administrator enabled deletion. Protected names such as \`main\`, \`master\`, \`dev\`, \`develop\` and \`release\`, and the project's base branch, are always refused. If the branch is already gone at the provider it counts as removed. Removed branches stay visible as history.

## If something fails

Authentication, TLS, timeout and rate-limit errors leave the branch record active, so you can retry. The message tells you which kind it was.
`,F=`# Admin setup

This guide is for admins and managers setting up a new workspace.

## 1. Create projects

In the sidebar, click **+** next to **Projects**. Give the project a name, a colour and a description. Tick **Enable Agile** if the team will use [Sprints](#help/sprints). You can also turn it on later from **Sprints**.

## 2. Add people

Click **+** next to **Team** and enter a name, an email, a role and a password. The password must meet the password policy. Under **Projects**, tag the projects this person can see. People only see the projects they belong to; admins see all of them. Untick **Active** to stop someone logging in without deleting their history.

## 3. Choose roles

See [Roles and permissions](#help/roles). Give people the least role that lets them work.

## 4. Check the basics

- **Audit Trail** lists every create, update, delete and login for incident review. It is open to managers and admins.
- **Sessions** (admins) lists devices that are signed in. Revoke one to log that device out without touching the others.
- Light and dark themes are in the account menu at the top right, along with **Change password**.

## 5. Optional features

These are switched on in the server's environment settings, not in the app:

- **Email**: choose the backend and, if you like, the daily digest.
- **Push**: needs a Firebase project and an https address. See [Notifications and push](#help/notifications).
- **Git branches**: per project, see [Git branches for stories](#help/git).
- **Sleuth** assistant: runs locally by default. A cloud model is optional.

## 6. Bring existing work in

Use **Download Template** and **Bulk Upload** on the Work Items page to import items from Excel or CSV. See [Creating and tracking items](#help/items).
`,H=`# Roles and permissions

There are three roles. Your role is shown in the account menu at the top right. The server checks it on every action, so a hidden button is never the only protection.

| Capability | User | Manager | Admin |
|---|---|---|---|
| See projects they belong to | Yes | Yes | All projects |
| Create and edit Bugs, Stories and Sub-tasks | Yes | Yes | Yes |
| Edit Tasks and Requirements | No | Yes | Yes |
| Edit any item or event | No | Yes | Yes |
| Move cards on a sprint board, view agile reports | Yes | Yes | Yes |
| Enable Agile, manage sprints, boards, releases, labels | No | Yes | Yes |
| Create projects and users | No | Yes | Yes |
| Use Reports and Audit Trail | No | Yes | Yes |
| Delete items, edit or delete comments, delete attachments | No | No | Yes |
| Grant the admin role, edit admins | No | No | Yes |
| View and revoke Sessions | No | No | Yes |

## Project access

Non-admins only see items in projects they are a member of. If something you expect is missing, ask a manager to add you to the project.

## Why a button might be missing

Sections such as Reports, Audit Trail and Sessions only appear for roles that may use them. If you need access, ask an admin to change your role.

See also [Admin setup](#help/admin).
`,V=Object.assign({"./guides/01-items.md":W,"./guides/02-sprints.md":L,"./guides/03-reports.md":U,"./guides/04-notifications.md":q,"./guides/05-git.md":M,"./guides/06-admin.md":F,"./guides/07-roles.md":H});function z(n){for(const e of n.split(/\n\s*\n/)){const a=e.trim();if(a&&!a.startsWith("#"))return a.replace(/\s+/g," ")}return""}const y=Object.entries(V).sort(([n],[e])=>n.localeCompare(e)).map(([n,e])=>{var o;const a=n.replace(/^.*\/\d*-?/,"").replace(/\.md$/,""),t=((o=/^#\s+(.+)$/m.exec(e))==null?void 0:o[1])??a;return{id:a,title:t,summary:z(e),markdown:e}});function v(n){return y.find(e=>e.id===n)??null}function K(n){const e=String(n??"").toLowerCase().split(/\s+/).filter(Boolean);return e.length?y.filter(a=>{const t=`${a.title}
${a.markdown}`.toLowerCase();return e.every(o=>t.includes(o))}):y}function te(){const[n,e]=d.useState(""),[a,t]=d.useState(()=>{var s;return E()??((s=y[0])==null?void 0:s.id)}),o=d.useRef(null),h=d.useRef(!1);d.useEffect(()=>$(()=>{const s=E();s&&v(s)&&(h.current=!0,t(s),e(""))}),[]);const c=d.useMemo(()=>K(n),[n]),i=v(a)??c[0]??y[0],l=d.useMemo(()=>i?O(i.markdown):"",[i]),p=d.useMemo(()=>i?B(i.markdown):[],[i]);d.useEffect(()=>{var s;h.current&&(h.current=!1,(s=o.current)==null||s.focus({preventScroll:!0}))},[i==null?void 0:i.id]);const u=s=>{h.current=!0,t(s)},m=s=>{var S,T,k,x;const g=(T=(S=s.target).closest)==null?void 0:T.call(S,"a[href]");if(!g)return;const f=g.getAttribute("href")||"";if(f.startsWith("#help/")){s.preventDefault();const j=f.slice(6);v(j)&&u(j)}else f.startsWith("#")&&(s.preventDefault(),(x=(k=document.getElementById(f.slice(1)))==null?void 0:k.scrollIntoView)==null||x.call(k,{block:"start"}))};return r.jsxs("section",{className:"help-center",id:"viewHelp","aria-label":"Help center",children:[r.jsxs("aside",{className:"help-side",children:[r.jsxs("div",{className:"field help-search",children:[r.jsx("label",{htmlFor:"helpSearch",className:"sr-only",children:"Search the guides"}),r.jsx("input",{id:"helpSearch",type:"search",placeholder:"Search the guides…",autoComplete:"off",value:n,onChange:s=>e(s.target.value)})]}),r.jsxs("nav",{"aria-label":"Guides",children:[r.jsx("ul",{className:"help-list",children:c.map(s=>r.jsx("li",{children:r.jsx("button",{type:"button",className:`help-list-item${s.id===(i==null?void 0:i.id)?" active":""}`,"aria-current":s.id===(i==null?void 0:i.id)?"page":void 0,onClick:()=>u(s.id),children:s.title})},s.id))}),c.length===0&&r.jsxs("p",{className:"help-none muted",role:"status",children:["No guide matches “",n,"”."]})]}),r.jsx("div",{className:"help-side-actions",children:r.jsx("button",{type:"button",className:"btn ghost",onClick:()=>I("tour"),children:"Take the product tour"})})]}),r.jsxs("article",{className:"help-article",ref:o,tabIndex:-1,"aria-label":i==null?void 0:i.title,children:[p.length>1&&r.jsx("nav",{className:"help-toc","aria-label":"On this page",children:p.filter(s=>s.level===2).map(s=>r.jsx("button",{type:"button",className:"help-toc-link",onClick:()=>{var g,f;return(f=(g=document.getElementById(s.id))==null?void 0:g.scrollIntoView)==null?void 0:f.call(g,{block:"start"})},children:s.text},s.id))}),r.jsx("div",{className:"help-md",onClick:m,role:"presentation",dangerouslySetInnerHTML:{__html:l}})]},i==null?void 0:i.id)]})}export{te as default};
