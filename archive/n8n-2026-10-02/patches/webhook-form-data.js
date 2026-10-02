"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.createMultiFormDataParser = void 0;
const formidable_1 = __importDefault(require("formidable"));
const promises_1 = require("node:fs/promises");
const bad_request_error_1 = require("../errors/response-errors/bad-request.error");
const content_too_large_error_1 = require("../errors/response-errors/content-too-large.error");
const webhook_blank_file_inputs_1 = require("../webhooks/webhook-blank-file-inputs");
const getFormidableHttpCode = (error) => {
    if (typeof error !== 'object' || error === null || !('httpCode' in error))
        return undefined;
    const { httpCode } = error;
    return typeof httpCode === 'number' ? httpCode : undefined;
};
const mapFormParseError = (error) => {
    switch (getFormidableHttpCode(error)) {
        case 413:
            return new content_too_large_error_1.ContentTooLargeError('The submitted form data exceeds the allowed size.');
        case 400:
            return bad_request_error_1.BadRequestError.wrap('The submitted form data could not be parsed.', error);
        default:
            return error instanceof Error ? error : new Error(String(error));
    }
};
const normalizeFormData = (values) => {
    for (const key in values) {
        const value = values[key];
        if (Array.isArray(value) && value.length === 1) {
            values[key] = value[0];
        }
    }
};
const createMultiFormDataParser = (maxFormDataSizeInMb) => {
    return async function parseMultipartFormData(req) {
        const { encoding } = req;
        const temporaryFilePaths = new Set();
        const form = (0, formidable_1.default)({
            multiples: true,
            encoding: encoding,
            maxFileSize: maxFormDataSizeInMb * 1024 * 1024,
            maxTotalFileSize: (Number(process.env.N8N_FORMDATA_TOTAL_SIZE_MAX) || 1024) * 1024 * 1024,
            allowEmptyFiles: true,
            minFileSize: 0,
        });
        form.on('fileBegin', (_formName, file) => temporaryFilePaths.add(file.filepath));
        const cleanup = async () => {
            await Promise.all([...temporaryFilePaths].map(async (filePath) => {
                try {
                    await (0, promises_1.rm)(filePath, { force: true });
                }
                catch {
                }
            }));
        };
        try {
            const [data, parsedFiles] = await form.parse(req);
            const files = await (0, webhook_blank_file_inputs_1.discardBlankFileInputs)(parsedFiles);
            normalizeFormData(data);
            normalizeFormData(files);
            return { body: { data, files }, cleanup };
        }
        catch (error) {
            await cleanup();
            throw mapFormParseError(error);
        }
    };
};
exports.createMultiFormDataParser = createMultiFormDataParser;
//# sourceMappingURL=webhook-form-data.js.map