/**
 * Route table.  Each view module exports `render(el, params, ctx)` which builds the page into
 * `el` and may return a cleanup function (called when navigating away).
 * `nav` entries define the sidebar; the order mirrors the product navigation (spec §36).
 */
export const ROUTES = [
  { path: "/night", view: "night", title: "Election Night", nav: "Live", icon: "night" },
  { path: "/president", view: "president", title: "President", nav: "Live", icon: "president" },
  { path: "/electoral-college", view: "electoral-college", title: "Electoral College", nav: "Live", icon: "college" },
  { path: "/provinces", view: "provinces", title: "Provinces", nav: "Results", icon: "provinces" },
  { path: "/provinces/:code", view: "province", title: "Province" },
  { path: "/municipalities", view: "municipalities", title: "Municipalities", nav: "Results", icon: "municipalities" },
  { path: "/municipalities/:code", view: "municipality", title: "Municipality" },
  { path: "/house", view: "house", title: "House", nav: "Results", icon: "house" },
  { path: "/house/:code", view: "district", title: "House district" },
  { path: "/senate", view: "senate", title: "Senate", nav: "Results", icon: "senate" },
  { path: "/governors", view: "governors", title: "Governors", nav: "Results", icon: "governors" },
  { path: "/races/:code", view: "race", title: "Race" },
  { path: "/forecast", view: "forecast", title: "Forecast", nav: "Analysis", icon: "forecast" },
  { path: "/polling", view: "polling", title: "Polling", nav: "Analysis", icon: "polling" },
  { path: "/campaign", view: "campaign", title: "Campaign", nav: "Analysis", icon: "campaign" },
  { path: "/history", view: "history", title: "History", nav: "Analysis", icon: "history" },
  { path: "/candidates/:id", view: "candidate", title: "Candidate" },
  { path: "/scenarios", view: "scenarios", title: "Scenario Editor", nav: "System", icon: "scenario" },
  { path: "/data", view: "data", title: "Data", nav: "System", icon: "data" },
  { path: "/settings", view: "settings", title: "Settings", nav: "System", icon: "settings" },
];

export const DEFAULT_ROUTE = "/night";
