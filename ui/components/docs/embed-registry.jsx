/* global React */

// ===========================================================================
// Task 1.2 -- embed registry.
//
// Maps every embed id to the real console component global that renders it,
// and to the fixture file stem that provides its data.
//
// USAGE
//   window.DocsEmbedRegistry["agents-page"]
//     => { component: "AgentsPage", fixtures: "agents-page",
//          props: { onOpen: () => {}, pushToast: () => {} } }
//
// window.DocsEmbedIds() returns the canonical id list (in sync with
// primer/user_docs/_fixtures/registry.json written below).
// ===========================================================================

(function () {
  // Each entry:
  //   component  - exact window.<name> the iframe renders
  //   fixtures   - stem of primer/user_docs/_fixtures/<stem>.json
  //   props      - static props to pass when rendering (callbacks are no-ops)
  //
  // Notes on sub-view ids:
  //   session-detail   -> SessionsList (page-level); SessionDetail requires a
  //                       specific sid prop; the sessions-list page naturally
  //                       shows the session from the fixture.
  //   workspace-template-form -> WorkspaceTemplatesPage (the template sub-page
  //                       that includes the create form).
  //   trigger-create   -> TR_TriggersPage (page-level list; TR_CreateTriggerDialog
  //                       is a modal that requires onClose/onCreated and cannot
  //                       render standalone without a parent).
  //   api-token-create -> AT_ApiTokensPage (page-level; AT_CreateTokenDialog is
  //                       a modal that requires onClose/onCreated/onDone).
  //   collection-list  -> CollectionsPage (page-level knowledge list).
  const REGISTRY = {
    "agents-page": {
      component: "AgentsPage",
      fixtures: "agents-page",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "sessions-list": {
      // Sessions live in the console shell (NV_Shell since the
      // three-view flag day; the sh shell died with it). The shell
      // reads the workspace from the hash rather than a prop, so the
      // hash names the workspace whose rows sessions-list.json holds.
      component: "NV_Shell",
      fixtures: "sessions-list",
      hash: "#/w/ws-blogassistant",
      props: {},
    },
    "session-detail": {
      // The same shell with one session open, so the capture shows the
      // transcript beside the rail.
      //
      // The session in session-detail.json is terminal (ended), so the
      // transcript renders fully from GET /sessions/{sid}/messages with
      // no live tap; the harness has no SSE backend.
      component: "NV_Shell",
      fixtures: "session-detail",
      hash: "#/w/ws-blogassistant?doc=session:sess-briefwriter",
      props: {},
    },
    // ---- the v2 shell ids the concept pages fence -------------------
    // These reuse the fixture files their surface already has: a fixture
    // is a recorded set of API responses, and two embeds looking at the
    // same surface want the same recording.
    "shell-session": {
      // The shell as an operator meets it: rail, tabs, one open session.
      component: "NV_Shell",
      fixtures: "session-detail",
      hash: "#/w/ws-blogassistant?doc=session:sess-briefwriter",
      props: { wid: "ws-blogassistant" },
    },
    "client-tool-open-file": {
      // The same workspace with a FILE open, which is what a client tool
      // delivering "open this" produces.
      component: "NV_Shell",
      fixtures: "session-detail",
      hash: "#/w/ws-blogassistant?doc=file:draft.md",
      props: { wid: "ws-blogassistant" },
    },
    "collections-tree": {
      // A collection open at its document tree: paths on the left, the
      // grep box above them.
      component: "CollectionsPage",
      fixtures: "collection-list",
      props: { pushToast: function () {} },
    },
    "providers-catalog": {
      // One catalog, every provider class on the rail.
      component: "ProviderCatalog",
      fixtures: "llm-provider-openrouter",
      props: { initialClass: "llm" },
    },
    "wizard-provider": {
      // First run, step 1: name the provider that will serve the models.
      component: "SetupWizardSteps",
      fixtures: "llm-provider-openrouter",
      props: { onComplete: function () {} },
    },
    "wizard-profile": {
      // First run, step 2: pick the default model. initialStep is why the
      // wizard takes one; the harness cannot click through to get here.
      component: "SetupWizardSteps",
      fixtures: "llm-provider-openrouter",
      props: {
        onComplete: function () {},
        initialStep: 2,
        initialModels: [
          { name: "gpt-4o-mini" },
          { name: "gpt-4o" },
          { name: "o4-mini" },
        ],
      },
    },
    "workspaces": {
      component: "WorkspacesPage",
      fixtures: "workspaces",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "workspace-template-form": {
      // WorkspaceTemplatesPage is the sub-page component that includes the
      // template create modal; WorkspacesPage embeds it as a tab.
      component: "WorkspaceTemplatesPage",
      fixtures: "workspace-template-form",
      props: { pushToast: function () {} },
    },
    "trigger-create": {
      component: "TR_TriggersPage",
      fixtures: "trigger-create",
      props: {},
    },
    "channels": {
      component: "ChannelsPage",
      fixtures: "channels",
      props: { onNavigate: function () {}, pushToast: function () {} },
    },
    "graph-canvas": {
      // GraphDetail (exposed on window alongside GraphsPage) renders the
      // single-graph editor whose centerpiece is the node/edge canvas
      // (GR_GraphEditor -> GR_Canvas). GraphsPage is the LIST page and never
      // shows the canvas, so the embed mounts the detail directly. graphId
      // must match the GET /graphs/{id} key in the fixture. Node x/y are
      // assigned client-side by primerVendor.autoLayout (server stores none).
      component: "GraphDetail",
      fixtures: "graph-canvas",
      props: { graphId: "docs-producer-judge", pushToast: function () {} },
    },
    "workers-stats": {
      component: "WorkersPage",
      fixtures: "workers-stats",
      props: { sessions: [], pushToast: function () {} },
    },
    "collection-list": {
      component: "CollectionsPage",
      fixtures: "collection-list",
      props: {
        pushToast: function () {},
        onOpen: function () {},
        onNavigate: function () {},
      },
    },
    "api-token-create": {
      component: "AT_ApiTokensPage",
      fixtures: "api-token-create",
      props: {},
    },
    "llm-provider-openrouter": {
      component: "ProviderCatalog",
      fixtures: "llm-provider-openrouter",
      props: { initialClass: "llm", onNavigate: function () {} },
    },
    "quickstart-agents": {
      component: "AgentsPage",
      fixtures: "quickstart-agents",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "internal-collections-enable": {
      component: "InternalCollectionsPage",
      fixtures: "internal-collections-enable",
      props: { pushToast: function () {} },
    },
    "quickstart-graph": {
      component: "GraphsPage",
      fixtures: "quickstart-graph",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "embedding-provider": {
      component: "ProviderCatalog",
      fixtures: "embedding-provider",
      props: { initialClass: "embedding", onNavigate: function () {} },
    },
    "ssp": {
      component: "SSPListPage",
      fixtures: "ssp",
      props: { pushToast: function () {} },
    },
    "cross-encoder-provider": {
      component: "ProviderCatalog",
      fixtures: "cross-encoder-provider",
      props: { initialClass: "cross_encoder", onNavigate: function () {} },
    },
    "web-search": {
      component: "ProviderCatalog",
      fixtures: "web-search",
      props: { initialClass: "web_search", onNavigate: function () {} },
    },
    "workspace-provider-create": {
      // The workspace providers list/create lives in WorkspaceProvidersPage
      // (ui/components/workspaces/providers.jsx); WorkspacesPage embeds it.
      component: "WorkspaceProvidersPage",
      fixtures: "workspace-provider-create",
      props: { pushToast: function () {} },
    },
    "channel-provider-create": {
      component: "ChannelProvidersPage",
      fixtures: "channel-provider-create",
      props: { onNavigate: function () {}, pushToast: function () {} },
    },
    "harness": {
      component: "HarnessesPage",
      fixtures: "harness",
      props: { pushToast: function () {} },
    },
    "mcp-exposure": {
      component: "MC_McpPage",
      fixtures: "mcp-exposure",
      props: { pushToast: function () {} },
    },
    "approvals": {
      component: "ApprovalsPage",
      fixtures: "approvals",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "toolsets": {
      component: "ToolsetsPage",
      fixtures: "toolsets",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "collection-create": {
      component: "CollectionsPage",
      fixtures: "collection-create",
      props: {
        pushToast: function () {},
        onOpen: function () {},
        onNavigate: function () {},
      },
    },
  };

  window.DocsEmbedRegistry = REGISTRY;

  window.DocsEmbedIds = function () {
    return Object.keys(REGISTRY);
  };
})();
