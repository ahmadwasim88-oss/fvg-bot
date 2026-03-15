addEventListener("fetch", event => {
  event.respondWith(handleRequest(event.request))
})

async function handleRequest(request) {
  var url = new URL(request.url)
  var binance = "https://fapi.binance.com" + url.pathname + url.search
  var response = await fetch(binance)
  var body = await response.text()
  return new Response(body, { headers: { "Content-Type": "application/json" } })
}
