// Dynamic Expo config: app.json stays the source of truth, this only layers the
// production web base path on top of it. `expo export` for the VPS runs with
// deploy.sh previously set EXPO_BASE_URL for a subpath; now shmax.praxisos.dev serves at root
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
