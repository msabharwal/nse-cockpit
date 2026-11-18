// Cloudflare Worker — NSE Cockpit API proxy
// Forwards requests to equityanalyze.com with CORS headers

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Only allow GET to our API
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, OPTIONS',
          'Access-Control-Allow-Headers': '*',
        }
      });
    }

    // Build the upstream URL — pass through all query params
    const upstream = 'https://api.equityanalyze.com/api/chartdatabyquery' + url.search;

    try {
      const resp = await fetch(upstream, {
        headers: {
          'Accept': 'application/json',
          'User-Agent': 'NSE-Cockpit/1.0',
        }
      });

      const body = await resp.text();

      return new Response(body, {
        status: resp.status,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
          'Cache-Control': 'no-cache',
        }
      });
    } catch (e) {
      return new Response(JSON.stringify({ result: 'error', error: { message: e.message } }), {
        status: 502,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
        }
      });
    }
  }
};
