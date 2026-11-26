// Botón para mostrar u ocultar cada campo de contraseña.
const icon = paths => `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const eye = icon('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>');
const eyeOff = icon('<path d="M10.7 5.1A10 10 0 0 1 12 5c6.500 0 10 7 10 7a17 17 0 0 1-2.200 3.100M6.600 6.600A17 17 0 0 0 2 12s3.500 7 10 7a10 10 0 0 0 5.400-1.600M9.900 9.900a3 3 0 0 0 4.200 4.200M3 3l18 18"/>');

document.querySelectorAll('input[type=password]').forEach(input => {
  const wrapper = document.createElement('span');
  wrapper.className = 'password-field';
  input.before(wrapper);

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'password-toggle';
  if (input.id) button.setAttribute('aria-controls', input.id);

  const show = visible => {
    input.type = visible ? 'text' : 'password';
    button.innerHTML = visible ? eyeOff : eye;
    button.setAttribute('aria-label', visible ? 'Ocultar contraseña' : 'Mostrar contraseña');
    button.title = button.getAttribute('aria-label');
    button.dataset.visible = visible;
  };
  // Mientras está visible no debe autocorregirse ni cambiar mayúsculas.
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('autocorrect', 'off');
  input.spellcheck = false;
  button.addEventListener('click', () => show(input.type === 'password'));
  // Se vuelve a ocultar al enviar, para que el navegador la trate como contraseña.
  input.form?.addEventListener('submit', () => show(false));
  input.addEventListener('password:hide', () => show(false));

  const focused = document.activeElement === input;
  wrapper.append(input, button);
  show(false);
  if (focused || input.autofocus) input.focus();
});
