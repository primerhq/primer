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
  //   chat-stream      -> ChatsPage (page-level list + detail stream).
  //   collection-list  -> CollectionsPage (page-level knowledge list).
  const REGISTRY = {
    "agents-page": {
      component: "AgentsPage",
      fixtures: "agents-page",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "sessions-list": {
      component: "SessionsList",
      fixtures: "sessions-list",
      props: {
        onOpenSession: function () {},
        onNewSession: function () {},
      },
    },
    "session-detail": {
      // SessionsList shows the list including the fixture session row;
      // session-detail.json is keyed by GET /sessions/{id} which maps to
      // that single session via the stub's query-insensitive fallback.
      // NOTE: rendered as SessionsList (page-level) because SessionDetail
      // requires an explicit sid prop to start fetching.
      component: "SessionsList",
      fixtures: "session-detail",
      props: {
        onOpenSession: function () {},
        onNewSession: function () {},
      },
    },
    "chat-stream": {
      component: "ChatsPage",
      fixtures: "chat-stream",
      props: { onOpen: function () {}, pushToast: function () {} },
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
      component: "GraphsPage",
      fixtures: "graph-canvas",
      props: { onOpen: function () {}, pushToast: function () {} },
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
        onSearchCollection: function () {},
        onNavigate: function () {},
      },
    },
    "api-token-create": {
      component: "AT_ApiTokensPage",
      fixtures: "api-token-create",
      props: {},
    },
    "llm-provider-openrouter": {
      component: "ProvidersPage",
      fixtures: "llm-provider-openrouter",
      props: { kind: "llm", pushToast: function () {} },
    },
    "quickstart-agents": {
      component: "AgentsPage",
      fixtures: "quickstart-agents",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "chat-agent-switch": {
      component: "ChatsPage",
      fixtures: "chat-agent-switch",
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
  };

  window.DocsEmbedRegistry = REGISTRY;

  window.DocsEmbedIds = function () {
    return Object.keys(REGISTRY);
  };
})();
