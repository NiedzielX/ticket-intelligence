import { launch } from "@cloudflare/playwright";

type Env = {
  BROWSER: any;
  SUPABASE_URL: string;
  SUPABASE_SECRET_KEY: string;
  ROBOTICKET_USERNAME?: string;
  ROBOTICKET_PASSWORD?: string;
  MANUAL_RUN_TOKEN?: string;
  EVENT_PROVIDER?: string;
  EVENT_HOME_TEAM?: string;
};

type TicketEvent = {
  id: number;
  external_event_id: string;
  home_team: string;
  away_team: string;
  competition: string | null;
  match_date: string;
  kickoff_at: string;
  source_url: string | null;
};

type Sector = {
  sector: string;
  available: number;
};

const SECTOR_INFO_ENDPOINT = "GetWGLSectorsInfo";

function headers(env: Env, prefer?: string): HeadersInit {
  const result: Record<string, string> = {
    apikey: env.SUPABASE_SECRET_KEY,
    Authorization: `Bearer ${env.SUPABASE_SECRET_KEY}`,
    Accept: "application/json",
    "Content-Type": "application/json",
  };
  if (prefer) result.Prefer = prefer;
  return result;
}

async function supabase(
  env: Env,
  path: string,
  init: RequestInit = {},
  prefer?: string,
): Promise<any> {
  const url = `${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/${path}`;
  const response = await fetch(url, {
    ...init,
    headers: {
      ...headers(env, prefer),
      ...(init.headers || {}),
    },
  });

  const text = await response.text();
  if (!response.ok) {
    throw new Error(`Supabase ${response.status}: ${text}`);
  }
  return text ? JSON.parse(text) : null;
}

async function loadActiveEvents(env: Env): Promise<TicketEvent[]> {
  const params = new URLSearchParams({
    provider: `eq.${env.EVENT_PROVIDER || "roboticket"}`,
    home_team: `eq.${env.EVENT_HOME_TEAM || "Lech Poznań"}`,
    kickoff_at: `gt.${new Date().toISOString()}`,
    select:
      "id,external_event_id,home_team,away_team,competition,match_date,kickoff_at,source_url",
    order: "kickoff_at.asc",
    limit: "20",
  });
  return (await supabase(env, `ticket_events?${params.toString()}`)) || [];
}

function parseSectors(data: any): Sector[] {
  const sectors: Sector[] = [];
  for (const sector of data?.sectors || []) {
    if (sector?.id == null) continue;
    const available = (sector.freeSeatsByPriceArea || []).reduce(
      (sum: number, area: any) => sum + Number(area?.freeSeatsNo || 0),
      0,
    );
    sectors.push({ sector: String(sector.id), available });
  }
  return sectors;
}

async function loginIfRequired(page: any, env: Env): Promise<void> {
  if (!page.url().includes("konto.legia.com")) return;

  if (!env.ROBOTICKET_USERNAME || !env.ROBOTICKET_PASSWORD) {
    throw new Error("Roboticket authentication required but credentials are not configured.");
  }

  console.log("Authentication required.");
  const email = page
    .locator('input[formcontrolname="email"], input[type="email"]')
    .first();
  const password = page
    .locator('input[formcontrolname="password"], input[type="password"]')
    .first();

  await email.waitFor({ state: "visible", timeout: 30_000 });
  await password.waitFor({ state: "visible", timeout: 30_000 });
  await email.fill(env.ROBOTICKET_USERNAME);
  await password.fill(env.ROBOTICKET_PASSWORD);

  const submit = page.getByRole("button", { name: /Zaloguj/i }).first();
  await submit.waitFor({ state: "visible", timeout: 30_000 });
  await submit.click();
  await page.waitForURL(
    (url: URL) => !url.toString().includes("konto.legia.com"),
    { timeout: 30_000 },
  );
}

async function createSnapshot(
  env: Env,
  event: TicketEvent,
  sectors: Sector[],
): Promise<{ snapshotId: number; availableTotal: number }> {
  const snapshots = await supabase(
    env,
    "snapshots",
    {
      method: "POST",
      body: JSON.stringify({
        event_id: null,
        source: env.EVENT_PROVIDER || "roboticket",
        ticket_event_id: event.id,
        event_match_date_at_capture: event.match_date,
        event_kickoff_at_capture: event.kickoff_at,
      }),
    },
    "return=representation",
  );

  if (!Array.isArray(snapshots) || snapshots.length !== 1) {
    throw new Error(`Expected one created snapshot for event ${event.external_event_id}.`);
  }

  const snapshotId = Number(snapshots[0].id);
  const inventoryRows = sectors.map((sector) => ({
    snapshot_id: snapshotId,
    event_id: null,
    sector: sector.sector,
    available: sector.available,
  }));

  try {
    await supabase(
      env,
      "sector_inventory",
      {
        method: "POST",
        body: JSON.stringify(inventoryRows),
      },
      "return=minimal",
    );
  } catch (error) {
    await supabase(env, `snapshots?id=eq.${snapshotId}`, { method: "DELETE" });
    throw error;
  }

  return {
    snapshotId,
    availableTotal: sectors.reduce((sum, sector) => sum + sector.available, 0),
  };
}

async function collectEvent(browser: any, env: Env, event: TicketEvent): Promise<any> {
  const eventUrl =
    event.source_url ||
    `https://bilety.lechpoznan.pl/Stadium/Index?eventId=${encodeURIComponent(event.external_event_id)}`;

  const context = await browser.newContext({
    locale: "pl-PL",
    timezoneId: "Europe/Warsaw",
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/126 Safari/537.36",
  });
  const page = await context.newPage();
  let sectorData: any = null;

  page.on("response", async (response: any) => {
    if (!response.url().includes(SECTOR_INFO_ENDPOINT)) return;
    try {
      sectorData = await response.json();
      console.log(
        `Captured sector response for ${event.external_event_id}: ${response.status()}`,
      );
    } catch {
      // Keep waiting for another valid sector response.
    }
  });

  try {
    console.log(`Opening ${event.external_event_id}: ${eventUrl}`);
    await page.goto(eventUrl, {
      waitUntil: "domcontentloaded",
      timeout: 90_000,
    });
    await page.waitForTimeout(1_500);

    await loginIfRequired(page, env);

    if (!page.url().includes(`eventId=${event.external_event_id}`)) {
      await page.goto(eventUrl, {
        waitUntil: "domcontentloaded",
        timeout: 90_000,
      });
    }

    for (let attempt = 0; attempt < 50 && sectorData == null; attempt += 1) {
      await page.waitForTimeout(500);
    }

    if (sectorData == null) {
      throw new Error(`GetWGLSectorsInfo not captured for ${event.external_event_id}.`);
    }

    const sectors = parseSectors(sectorData);
    if (!sectors.length) {
      throw new Error(`No sectors returned for ${event.external_event_id}.`);
    }

    const snapshot = await createSnapshot(env, event, sectors);
    const result = {
      provider_event_id: event.external_event_id,
      ticket_event_id: event.id,
      away_team: event.away_team,
      competition: event.competition,
      kickoff_at: event.kickoff_at,
      snapshot_id: snapshot.snapshotId,
      sector_count: sectors.length,
      available_total: snapshot.availableTotal,
    };
    console.log(JSON.stringify(result));
    return result;
  } finally {
    await page.close().catch(() => undefined);
    await context.close().catch(() => undefined);
  }
}

async function runCollector(env: Env): Promise<any> {
  const events = await loadActiveEvents(env);
  console.log(`Active future events: ${events.length}`);

  if (!events.length) {
    return { status: "no_active_events", events: [] };
  }

  const browser = await launch(env.BROWSER);
  const results: any[] = [];
  const failures: any[] = [];

  try {
    for (const event of events) {
      try {
        results.push(await collectEvent(browser, env, event));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`Collection failed for ${event.external_event_id}: ${message}`);
        failures.push({
          provider_event_id: event.external_event_id,
          away_team: event.away_team,
          error: message,
        });
      }
    }
  } finally {
    await browser.close().catch(() => undefined);
  }

  const summary = {
    status: failures.length ? "partial_failure" : "success",
    collected: results.length,
    failed: failures.length,
    results,
    failures,
  };
  console.log(JSON.stringify(summary));

  if (failures.length) {
    throw new Error(`Collector finished with ${failures.length} failed event(s).`);
  }
  return summary;
}

export default {
  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    await runCollector(env);
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return Response.json({ status: "ok", service: "beyond-ticketing-emergency-collector" });
    }

    if (url.pathname === "/run") {
      if (
        !env.MANUAL_RUN_TOKEN ||
        request.headers.get("Authorization") !== `Bearer ${env.MANUAL_RUN_TOKEN}`
      ) {
        return new Response("Unauthorized", { status: 401 });
      }
      try {
        return Response.json(await runCollector(env));
      } catch (error) {
        return Response.json(
          { status: "error", error: error instanceof Error ? error.message : String(error) },
          { status: 500 },
        );
      }
    }

    return new Response("Not found", { status: 404 });
  },
};
