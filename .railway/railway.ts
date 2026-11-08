import { defineRailway, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const jobHunterVolume = volume("job-hunter-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 5000 });
  const jobHunter = service("job-hunter", {
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    replicas: { "sfo": 1 },
    volumeMounts: { "/data": jobHunterVolume },
    env: { MINIAPP_DEV_FALLBACK: preserve(), PERSONAL_BOT_TOKEN: preserve(), WEBAPP_URL: preserve() },
  });

  return project("job-hunter", {
    variables: { managed: true },
    resources: [jobHunter, jobHunterVolume],
  });
});
