// Studio2 command registry. ONE table feeds palette, menus, and chords,
// so a shortcut is never defined twice (spec section 7).
(function () {
  const cmds = new Map();
  window.S2_MOD = /mac/i.test(navigator.platform) ? "⌘" : "Ctrl";
  window.S2_Commands = {
    register(cmd) {
      if (!cmd || !cmd.id || typeof cmd.run !== "function") {
        throw new Error("S2_Commands.register: id and run are required");
      }
      cmds.set(cmd.id, cmd);
    },
    list() {
      return [...cmds.values()].filter((c) => !c.when || c.when());
    },
    run(id) {
      const c = cmds.get(id);
      if (c && (!c.when || c.when())) c.run();
    },
  };
})();
