type Env = {
  SUPABASE_URL: string;
  SUPABASE_SECRET_KEY: string;
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

const ROBOTICKET_ORIGIN = "https://bilety.lechpoznan.pl";
const ROBOTICKET_BASE_URL = `${ROBOTICKET_ORIGIN}/Stadium`;
const SECTOR_INFO_ENDPOINT = "GetWGLSectorsInfo";
const BROWSER_USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

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

function getSetCookieValues(headers: Headers): string[] {
  const anyHeaders = headers as any;
  if (typeof anyHeaders.getSetCookie === "function") {
    return anyHeaders.getSetCookie();
  }
  if (typeof anyHeaders.getAll === "function") {
    try {
      return anyHeaders.getAll("Set-Cookie") || [];
    } catch {
      // Fall through to the combined header representation.
    }
  }
  const combined = headers.get("Set-Cookie");
  if (!combined) return [];
  return combined.split(/,(?=\s*[^;,=\s]+=)/g);
}

function applySetCookies(cookieJar: Map<string, string>, setCookies: string[]): void {
  for (const setCookie of setCookies) {
    const firstPart = setCookie.split(";", 1)[0]?.trim();
    if (!firstPart) continue;
    const separator = firstPart.indexOf("=");
    if (separator <= 0) continue;
    const name = firstPart.slice(0, separator).trim();
    const value = firstPart.slice(separator + 1).trim();
    if (/max-age=0/i.test(setCookie) || !value) {
      cookieJar.delete(name);
    } else {
      cookieJar.set(name, value);
    }
  }
}

function cookieHeader(cookieJar: Map<string, string>): string {
  return Array.from(cookieJar.entries())
    .map(([name, value]) => `${name}=${value}`)
    .join("; ");
}

async function bootstrapRoboticketSession(eventUrl: string): Promise<Map<string, string>> {
  const cookies = new Map<string, string>();
  let currentUrl = eventUrl;

  for (let redirect = 0; redirect < 6; redirect += 1) {
    const response = await fetch(currentUrl, {
      method: "GET",
      redirect: "manual",
      headers: {
        Accept:
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        Pragma: "no-cache",
        "User-Agent": BROWSER_USER_AGENT,
        ...(cookies.size ? { Cookie: cookieHeader(cookies) } : {}),
      },
    });

    applySetCookies(cookies, getSetCookieValues(response.headers));

    const location = response.headers.get("Location");
    if (response.status >= 300 && response.status < 400 && location) {
      currentUrl = new URL(location, currentUrl).toString();
      await response.body?.cancel().catch(() => undefined);
      continue;
    }

    await response.body?.cancel().catch(() => undefined);

    if (!response.ok) {
      throw new Error(
        `Roboticket session bootstrap ${response.status} at ${new URL(currentUrl).host}.`,
      );
    }

    if (!currentUrl.startsWith(ROBOTICKET_ORIGIN)) {
      throw new Error(
        `Roboticket session bootstrap left ticket host: ${new URL(currentUrl).host}.`,
      );
    }

    console.log(
      `Roboticket session ready: status=${response.status}, cookies=${Array.from(cookies.keys()).join(",") || "none"}`,
    );
    return cookies;
  }

  throw new Error("Roboticket session bootstrap exceeded redirect limit.");
}

async function fetchSectorInfo(event: TicketEvent): Promise<any> {
  const eventId = encodeURIComponent(event.external_event_id);
  const endpoint = `${ROBOTICKET_BASE_URL}/${SECTOR_INFO_ENDPOINT}?eventId=${eventId}`;
  const eventUrl =
    event.source_url || `${ROBOTICKET_BASE_URL}/Index?eventId=${eventId}`;

  const cookies = await bootstrapRoboticketSession(eventUrl);
  console.log(`Fetching ${event.external_event_id}: ${endpoint}`);

  const response = await fetch(endpoint, {
    method: "GET",
    redirect: "manual",
    headers: {
      Accept: "application/json, text/plain, */*",
      "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
      "Cache-Control": "no-cache",
      Pragma: "no-cache",
      Referer: eventUrl,
      "User-Agent": BROWSER_USER_AGENT,
      "X-Requested-With": "XMLHttpRequest",
      ...(cookies.size ? { Cookie: cookieHeader(cookies) } : {}),
    },
  });

  applySetCookies(cookies, getSetCookieValues(response.headers));
  const text = await response.text();
  const contentType = response.headers.get("Content-Type") || "unknown";
  const location = response.headers.get("Location");

  if (!response.ok || (response.status >= 300 && response.status < 400)) {
    throw new Error(
      `Roboticket ${SECTOR_INFO_ENDPOINT} status=${response.status}, contentType=${contentType}, location=${location || "none"}, bodyLength=${text.length} for ${event.external_event_id}: ${text.slice(0, 300)}`,
    );
  }

  let data: any;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(
      `Roboticket ${SECTOR_INFO_ENDPOINT} returned non-JSON: status=${response.status}, contentType=${contentType}, bodyLength=${text.length}, cookies=${Array.from(cookies.keys()).join(",") || "none"} for ${event.external_event_id}: ${text.slice(0, 300)}`,
    );
  }

  if (!Array.isArray(data?.sectors)) {
    throw new Error(
      `Roboticket ${SECTOR_INFO_ENDPOINT} returned no sectors for ${event.external_event_id}.`,
    );
  }

  console.log(
    `Roboticket sector response for ${event.external_event_id}: ${response.status}, sectors=${data.sectors.length}, contentType=${contentType}`,
  );
  return data;
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

async function collectEvent(env: Env, event: TicketEvent): Promise<any> {
  const sectorData = await fetchSectorInfo(event);
  const sectors = parseSectors(sectorData);
  if (!sectors.length) {
    throw new Error(`No sector inventory returned for ${event.external_event_id}.`);
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
}

async function runCollector(env: Env): Promise<any> {
  const events = await loadActiveEvents(env);
  console.log(`Active future events: ${events.length}`);

  if (!events.length) {
    return { status: "no_active_events", events: [] };
  }

  const results: any[] = [];
  const failures: any[] = [];

  for (const event of events) {
    try {
      results.push(await collectEvent(env, event));
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
