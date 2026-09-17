// Dynamic Expo config: app.json stays the source of truth, this only layers the
// production web base path on top of it. `expo export` for the VPS runs with
// EXPO_BASE_URL=/shmax (set by deploy.sh); local dev and native builds leave it unset
// and therefore behave exactly as before.
module.exports = ({ config }) => {
  const baseUrl = process.env.EXPO_BASE_URL;

  if (!baseUrl) {
    return config;
  }

  return {
    ...config,
    experiments: {
      ...(config.experiments ?? {}),
      baseUrl,
    },
  };
};
