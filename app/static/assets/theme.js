(() => {
  const saved=localStorage.getItem('nai-gate-theme');
  let mode=['light','dark','system'].includes(saved)?saved:'system';
  const system=matchMedia('(prefers-color-scheme: dark)');
  function apply(){
    document.documentElement.dataset.theme=mode==='system'?(system.matches?'dark':'light'):mode;
    document.documentElement.dataset.themeMode=mode;
    document.querySelectorAll('[data-theme-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.themeMode===mode)));
  }
  document.querySelectorAll('[data-theme-mode]').forEach(button=>button.addEventListener('click',()=>{mode=button.dataset.themeMode;localStorage.setItem('nai-gate-theme',mode);apply();}));
  system.addEventListener('change',()=>{if(mode==='system')apply();});
  apply();
})();
