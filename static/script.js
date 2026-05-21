// ── CourtIQ JS ──────────────────────────────────────────────────

// Range slider live value display
document.querySelectorAll('input[type="range"]').forEach(slider => {
    const output = document.getElementById(slider.id + '_val');
    if (output) {
        output.textContent = slider.value;
        slider.addEventListener('input', () => {
            output.textContent = slider.value;
        });
    }
});

// Height calculator helper
const heightFeet = document.getElementById('height_feet');
const heightInches = document.getElementById('height_inches_part');
const heightTotal = document.getElementById('height_inches');

function updateHeight() {
    if (heightFeet && heightInches && heightTotal) {
        const ft = parseInt(heightFeet.value) || 0;
        const inches = parseInt(heightInches.value) || 0;
        heightTotal.value = ft * 12 + inches;
    }
}

if (heightFeet) heightFeet.addEventListener('change', updateHeight);
if (heightInches) heightInches.addEventListener('change', updateHeight);

// Wingspan calculator
const wsFeet = document.getElementById('ws_feet');
const wsInches = document.getElementById('ws_inches_part');
const wsTotal = document.getElementById('wingspan_inches');

function updateWingspan() {
    if (wsFeet && wsInches && wsTotal) {
        const ft = parseInt(wsFeet.value) || 0;
        const inches = parseInt(wsInches.value) || 0;
        wsTotal.value = ft * 12 + inches;
    }
}

if (wsFeet) wsFeet.addEventListener('change', updateWingspan);
if (wsInches) wsInches.addEventListener('change', updateWingspan);

// Confirm delete
document.querySelectorAll('.delete-form').forEach(form => {
    form.addEventListener('submit', e => {
        if (!confirm('Are you sure? This cannot be undone.')) {
            e.preventDefault();
        }
    });
});
