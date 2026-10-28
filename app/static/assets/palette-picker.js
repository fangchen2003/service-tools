(() => {
  const palettes = [
    { id: 'colorful', name: '缤纷' },
    { id: 'warm', name: '暖灰' },
    { id: 'blue', name: '冷雾蓝' },
    { id: 'rose-pine', name: 'Rosé Pine' },
  ];
  const root = document.documentElement;
  const dialog = document.getElementById('paletteDialog');
  const trigger = document.getElementById('palettePicker');
  const options = document.getElementById('paletteOptions');
  const sample = (palette, theme) => `<span class="palette-sample" data-palette="${palette}" data-theme="${theme}" aria-hidden="true"><span class="sample-sidebar"><i></i><i></i><i></i></span><span class="sample-main"><span class="sample-heading"></span><span class="sample-stats"><i></i><i></i></span><span class="sample-chart"><i></i><i></i><i></i><i></i><i></i></span></span></span>`;
  options.innerHTML = palettes.map(palette => `<button class="palette-card" type="button" data-choice="${palette.id}" aria-pressed="false"><span class="palette-samples">${sample(palette.id, 'light')}${sample(palette.id, 'dark')}</span><span class="palette-card-heading"><strong>${palette.name}</strong><span class="palette-selected" aria-hidden="true">✓</span></span></button>`).join('');
  function selectPalette(id, updateUrl) {
    const palette = palettes.find(item => item.id === id) || palettes[0];
    root.dataset.palette = palette.id;
    document.getElementById('paletteName').textContent = palette.name;
    trigger.setAttribute('aria-label', `选择配色，当前${palette.name}`);
    options.querySelectorAll('[data-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.choice === palette.id)));
    if (updateUrl) {
      localStorage.setItem('nai-gate-palette', palette.id);
      const url = new URL(location.href);
      if (palette.id === 'colorful') url.searchParams.delete('palette');
      else url.searchParams.set('palette', palette.id);
      history.replaceState(null, '', url);
    }
  }
  selectPalette(new URLSearchParams(location.search).get('palette') || localStorage.getItem('nai-gate-palette'), false);
  trigger.addEventListener('click', () => dialog.showModal());
  dialog.querySelector('.palette-close').addEventListener('click', () => dialog.close());
  options.addEventListener('click', event => {
    const button = event.target.closest('[data-choice]');
    if (!button) return;
    selectPalette(button.dataset.choice, true);
    dialog.close();
  });
})();
