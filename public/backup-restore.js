/* Platform-admin backup actions are deliberately only mounted in the root database workspace. */
(() => {
  window.downloadDatabaseBackup = async () => {
    try {
      databaseBackupResult.textContent = "Preparing backup…";
      const backup = await api("/api/database/backup");
      const blob = new Blob([JSON.stringify(backup, null, 2)], {type:"application/json"});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `cmdb-hub-backup-${backup.createdAt.slice(0, 10)}.json`;
      link.click(); URL.revokeObjectURL(link.href);
      databaseBackupResult.innerHTML = `<span class="success">Backup downloaded.</span> ${esc(backup.state.companies.length)} customers · ${esc(backup.state.assets.length)} assets`;
    } catch (error) { databaseBackupResult.innerHTML = `<span class="blocked">Backup failed:</span> ${esc(error.message)}`; }
  };

  window.restoreDatabaseBackup = async event => {
    event.preventDefault();
    const file = databaseRestoreFile.files[0];
    if (!file) return;
    if (!confirm(`Restore ${file.name}? This replaces the current CMDB state and cannot be undone.`)) return;
    try {
      databaseBackupResult.textContent = "Validating and restoring backup…";
      const backup = JSON.parse(await file.text());
      const result = await api("/api/database/restore", {method:"POST", body:JSON.stringify(backup)});
      databaseRestoreFile.value = ""; databaseRestoreConfirm.checked = false;
      databaseBackupResult.innerHTML = `<span class="success">${esc(result.message)}</span> ${esc(result.companies)} customers · ${esc(result.assets)} assets`;
      await refresh();
    } catch (error) { databaseBackupResult.innerHTML = `<span class="blocked">Restore failed:</span> ${esc(error.message)}`; }
  };
})();
