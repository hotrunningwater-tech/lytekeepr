const navToggle = document.getElementById('navToggle');
const landingNav = document.getElementById('landingNav');

if (navToggle && landingNav) {
  navToggle.addEventListener('click', () => {
    const isOpen = landingNav.classList.toggle('is-open');
    navToggle.setAttribute('aria-expanded', String(isOpen));
  });

  landingNav.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => {
      landingNav.classList.remove('is-open');
      navToggle.setAttribute('aria-expanded', 'false');
    });
  });
}

const form = document.getElementById('earlyAccessForm');
const confirmation = document.getElementById('formConfirmation');
const formError = document.getElementById('formError');
const submitBtn = document.getElementById('earlyAccessSubmitBtn');

if (form && confirmation) {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    formError.hidden = true;
    confirmation.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Sending…';

    const data = Object.fromEntries(new FormData(form).entries());

    try {
      const res = await fetch('/api/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) throw new Error('request failed');

      confirmation.hidden = false;
      form.reset();
      confirmation.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
      formError.hidden = false;
      formError.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Request Access';
    }
  });
}

const pricingAnnualBtn = document.getElementById('pricingAnnualBtn');
const pricingMonthlyBtn = document.getElementById('pricingMonthlyBtn');
const pricingPrice = document.getElementById('pricingPrice');
const pricingNote = document.getElementById('pricingNote');

if (pricingAnnualBtn && pricingMonthlyBtn && pricingPrice && pricingNote) {
  const plans = {
    annual: { price: '$39', note: 'Billed annually' },
    monthly: { price: '$49', note: 'Billed monthly' },
  };

  const setPeriod = (period) => {
    pricingPrice.textContent = plans[period].price;
    pricingNote.textContent = plans[period].note;
    pricingAnnualBtn.classList.toggle('is-active', period === 'annual');
    pricingMonthlyBtn.classList.toggle('is-active', period === 'monthly');
  };

  pricingAnnualBtn.addEventListener('click', () => setPeriod('annual'));
  pricingMonthlyBtn.addEventListener('click', () => setPeriod('monthly'));
}
