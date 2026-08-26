import '@testing-library/jest-dom/vitest';

import { configure } from '@testing-library/react';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Testing Library's 1s default is tight for the first test in a cold jsdom
// worker, where module transform and environment setup dominate. Raising it
// removes a class of false failures without hiding a genuinely stuck render.
configure({ asyncUtilTimeout: 5_000 });

afterEach(cleanup);
