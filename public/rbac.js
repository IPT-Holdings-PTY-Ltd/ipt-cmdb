/* Root-only RBAC reference and effective-access preview. */
(() => {
  window.loadEffectiveAccess = async () => {
    if (!rbacUser.value) return;
    const result = await api(`/api/rbac/effective?userId=${encodeURIComponent(rbacUser.value)}`);
    effectiveAccess.innerHTML = `<h3>${esc(result.user.email)} · ${esc(result.role.name)}</h3><p><b>Customer scope:</b> ${esc(result.scope)}</p><h4>Allowed actions</h4>${result.role.permissions.map(permission => `<p class="relation"><span>✓</span>${esc(permission)}</p>`).join("")}`;
  };
  window.loadRbac = async () => {
    if (!isRootContext() || me?.role !== "platform_admin") return;
    const [roles, users] = await Promise.all([api("/api/rbac/roles"), api("/api/users")]);
    roleCards.innerHTML = roles.map(role => `<article><h3>${esc(role.name)}</h3><p>${esc(role.scope)}</p><small>${role.permissions.map(esc).join(" · ")}</small></article>`).join("");
    rbacUser.innerHTML = users.map(user => `<option value="${user.id}">${esc(user.email)} — ${esc(user.role)}</option>`).join("");
    await loadEffectiveAccess();
  };
  const baseSync = window.syncRootAdminNavigation;
  window.syncRootAdminNavigation = () => { baseSync?.(); document.querySelector('[data-page="rbac"]').hidden = !(isRootContext() && me?.role === "platform_admin"); };
  const baseShowPage = showPage;
  showPage = id => { baseShowPage(id); if (id === "rbac") { pageTitle.textContent = "Access control"; loadRbac(); } };
})();
