(() => {
  const sidebar = document.querySelector('.gate-sidebar');
  const toggle = document.getElementById('sidebarToolsToggle');
  const collapse = document.getElementById('sidebarCollapse');
  function setCollapsed(collapsed) {
    document.body.classList.toggle('is-sidebar-collapsed', collapsed);
    collapse.setAttribute('aria-expanded', String(!collapsed));
    collapse.setAttribute('aria-label', collapsed ? '展开侧栏' : '收起侧栏');
    collapse.title = collapsed ? '展开侧栏' : '收起侧栏';
    setOpen(false);
  }
  function setOpen(open) {
    sidebar.classList.toggle('is-tools-open', open);
    toggle.setAttribute('aria-expanded', String(open));
  }
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
  collapse.addEventListener('click', () => {
    const collapsed = !document.body.classList.contains('is-sidebar-collapsed');
    setCollapsed(collapsed);
    localStorage.setItem('nai-gate-sidebar-collapsed', String(collapsed));
  });
  setCollapsed(localStorage.getItem('nai-gate-sidebar-collapsed') === 'true');
  document.addEventListener('click', event => {
    if (!sidebar.contains(event.target) || event.target.closest('.gate-navigation')) setOpen(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
      setOpen(false);
      toggle.focus();
    }
  });
})();
