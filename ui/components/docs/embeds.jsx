/* global React */

// Embed registry. Each mockup is a small React component file under
// ui/components/docs/embeds/; those files load BEFORE this file in
// index.html and attach their component to window.<Name>Mockup. The
// mockup:<id> directive (directives-mockup.jsx) resolves ids against
// window.DocsEmbeds.
//
// The lint engine (rule 3) gets the list of valid ids through the
// backend manifest endpoint /v1/user_docs/embeds/manifest, which is
// seeded by app.state.user_docs_embeds. Keep that list in lockstep
// with the keys below when adding a new embed.

const EMBEDS = {
  "topbar":                   window.TopbarMockup,
  "sessions-list-empty":      window.SessionsListEmptyMockup,
  "agent-create-modal":       window.AgentCreateModalMockup,
  "graph-canvas-three-nodes": window.GraphCanvasThreeNodesMockup,
  "channels-prompt":          window.ChannelsPromptMockup,
  "docs-callout-demo":        window.DocsCalloutDemoMockup,
};

window.DocsEmbeds = EMBEDS;
window.DocsEmbedIds = () => Object.keys(EMBEDS);
