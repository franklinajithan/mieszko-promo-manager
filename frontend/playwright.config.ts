import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./e2e',timeout:60000,workers:1,use:{baseURL:process.env.E2E_BASE_URL||'http://127.0.0.1:8000',headless:true,screenshot:'only-on-failure',trace:'retain-on-failure'},reporter:[['list'],['html',{open:'never'}]]});
