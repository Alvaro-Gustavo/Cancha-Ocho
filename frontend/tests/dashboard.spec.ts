import { expect, test } from '@playwright/test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { resolve } from 'node:path';

for (const width of [360, 768, 1440]) {
  test(`consulta privada y matriz a ${width}px`, async ({ page }) => {
    if (process.env.CANCHA_ASGI_BRIDGE === '1') {
      const root = resolve('..');
      const bridge = spawn(process.env.CANCHA_PYTHON || 'python', ['-u', 'tests/browser_bridge.py'], {
        cwd: root, env: { ...process.env, PYTHONPATH: root, PYTHONUTF8: '1' }, stdio: ['pipe', 'pipe', 'inherit'],
      });
      page.on('close', () => { bridge.stdin.end(); bridge.kill(); });
      const lines = createInterface({ input: bridge.stdout });
      expect((await once(lines, 'line'))[0]).toBe('ready');
      let queue = Promise.resolve();
      await page.route('**/*', route => queue = queue.then(async () => {
        const request = route.request();
        const reply = once(lines, 'line');
        bridge.stdin.write(JSON.stringify({ path: new URL(request.url()).pathname, method: request.method(),
          headers: await request.allHeaders(), body: request.postData() || '' }) + '\n');
        const data = JSON.parse((await reply)[0]);
        await route.fulfill({ status: data.status, headers: data.headers, body: Buffer.from(data.body, 'base64') });
      }));
    }
    await page.setViewportSize({ width, height: 960 });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Panel privado' })).toBeVisible();
    await page.getByLabel('Contraseña').fill(process.env.CANCHA_TEST_PASSWORD ?? 'cancha-test-local-2026');
    await page.getByLabel('Contraseña').press('Enter');
    await expect(page.getByRole('heading', { name: 'Pronósticos', exact: true })).toBeVisible();
    await expect(page.getByText('Datos de demostración')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Probabilidades 1X2' })).toBeVisible();
    const active = page.locator('.fixture[aria-pressed=true]');
    const label = await active.textContent();
    const data = await page.evaluate(async () => (await fetch('/api/dashboard')).json());
    const fixture = data.fixtures.find((match: { home: string; away: string }) => label?.includes(match.home) && label?.includes(match.away));
    const forecast = await page.evaluate(async id => (await fetch(`/api/forecast/${id}`)).json(), fixture.id);
    const format = new Intl.NumberFormat('es-BO', { style: 'percent', maximumFractionDigits: 1 });
    expect(await page.locator('.probabilities strong').allTextContents()).toEqual(forecast.payload.probabilities.map((value: number) => format.format(value)));
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    const matrix = page.getByRole('button', { name: 'Ver matriz completa' });
    await matrix.focus();
    await matrix.press('Enter');
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    if (width === 1440 && process.env.CANCHA_CAPTURE) {
      await page.screenshot({ path: process.env.CANCHA_CAPTURE, animations: 'disabled' });
    }
    await page.getByLabel('Competición', { exact: true }).selectOption('CL');
    await expect(page.locator('.fixture')).toHaveCount(3);
    const pending = page.locator('.fixture').filter({ hasText: 'Pendiente' });
    await pending.click();
    await expect(page.getByRole('heading', { name: 'Pronóstico no disponible' })).toBeVisible();
    if (width < 731) await page.getByRole('button', { name: 'Menú' }).click();
    await page.getByRole('button', { name: 'Fuentes de datos' }).click();
    await expect(page.getByRole('heading', { name: 'Importación por CSV' })).toBeVisible();
    if (width < 731) await page.getByRole('button', { name: 'Menú' }).click();
    await page.getByRole('button', { name: 'Evaluación de modelos' }).click();
    await expect(page.getByRole('heading', { name: 'Evaluación retrospectiva' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Emisiones reales a ocho horas' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
  });
}
