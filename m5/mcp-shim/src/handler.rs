use std::borrow::Cow;
use std::io;
use std::path::PathBuf;
use std::time::Duration;

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ErrorData, JsonObject,
    ListToolsResult, PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerConfig,
    Tool,
};
use rmcp::service::RequestContext;
use rmcp::{RoleServer, ServerHandler};
use tokio::io::AsyncWriteExt;
use tokio::net::UnixStream;
use tokio::time::{timeout_at, Instant};
use writ_hostsys::sock::{peer_credentials, recv_plain};
use writ_kernel::{ids, json as wire};
use writ_rpc::frame::{self, Frame, FrameLimits, FrameType};

use crate::lifecycle::Admission;

const TOOL: &str = "fs.write";
const RPC_TIMEOUT: Duration = Duration::from_secs(5);
const PATH_LIMIT: usize = 4096;
const KEY_LIMIT: usize = 200;
const FIELDS: [&str; 6] = [
    "relative_path",
    "source_path",
    "snapshot_id",
    "manifest_digest",
    "content_digest",
    "idempotency_key",
];

pub(crate) struct Handler {
    admission: Admission,
    endpoint: Option<Endpoint>,
    #[cfg(test)]
    before_call: Pause,
    #[cfg(test)]
    before_submit: Pause,
}

struct Endpoint {
    socket: PathBuf,
    service_uid: u32,
}

#[cfg(test)]
#[derive(Default)]
struct Pause(
    std::sync::Mutex<
        Option<(
            tokio::sync::oneshot::Sender<()>,
            tokio::sync::oneshot::Receiver<()>,
        )>,
    >,
);

#[cfg(test)]
impl Pause {
    async fn run(&self) {
        let next = self.0.lock().ok().and_then(|mut pause| pause.take());
        if let Some((entered, resume)) = next {
            let _ = entered.send(());
            let _ = resume.await;
        }
    }
}

impl Handler {
    pub(crate) fn closed(admission: Admission) -> Self {
        Self {
            admission,
            endpoint: None,
            #[cfg(test)]
            before_call: Pause::default(),
            #[cfg(test)]
            before_submit: Pause::default(),
        }
    }

    #[cfg(test)]
    pub(crate) fn recording(admission: Admission, socket: PathBuf, service_uid: u32) -> Self {
        Self {
            endpoint: Some(Endpoint {
                socket,
                service_uid,
            }),
            ..Self::closed(admission)
        }
    }

    #[cfg(test)]
    pub(crate) fn pause_before_call(
        mut self,
        entered: tokio::sync::oneshot::Sender<()>,
        resume: tokio::sync::oneshot::Receiver<()>,
    ) -> Self {
        self.before_call = Pause(std::sync::Mutex::new(Some((entered, resume))));
        self
    }

    #[cfg(test)]
    pub(crate) fn pause_before_submit(
        mut self,
        entered: tokio::sync::oneshot::Sender<()>,
        resume: tokio::sync::oneshot::Receiver<()>,
    ) -> Self {
        self.before_submit = Pause(std::sync::Mutex::new(Some((entered, resume))));
        self
    }

    fn admitted(&self, context: &RequestContext<RoleServer>) -> bool {
        !context.ct.is_cancelled() && self.admission.allowed(&context.id)
    }

    async fn forward(
        &self,
        endpoint: &Endpoint,
        request: &[u8],
        context: &RequestContext<RoleServer>,
    ) -> Outcome {
        let deadline = Instant::now() + RPC_TIMEOUT;
        let connected = tokio::select! {
            biased;
            () = context.ct.cancelled() => return Outcome::NotSubmitted,
            result = timeout_at(deadline, UnixStream::connect(&endpoint.socket)) => result,
        };
        let Ok(Ok(mut stream)) = connected else {
            return Outcome::NotSubmitted;
        };
        let Ok(peer) = peer_credentials(&stream) else {
            return Outcome::NotSubmitted;
        };
        if endpoint.service_uid == 0
            || !writ_rpc::verify_server(
                &writ_rpc::PeerCred {
                    uid: peer.uid,
                    gid: peer.gid,
                    pid: peer.pid,
                },
                endpoint.service_uid,
            )
        {
            return Outcome::NotSubmitted;
        }
        let ready = tokio::select! {
            biased;
            () = context.ct.cancelled() => return Outcome::NotSubmitted,
            result = timeout_at(deadline, stream.writable()) => result,
        };
        if !matches!(ready, Ok(Ok(()))) {
            return Outcome::NotSubmitted;
        }

        #[cfg(test)]
        self.before_submit.run().await;

        // Serialize the nonblocking first write with typed receive admission,
        // including cancellation and EOF observed before the SDK dispatches them.
        let sent = self.admission.with_submission(&context.id, || {
            if context.ct.is_cancelled() || Instant::now() >= deadline {
                return None;
            }
            stream.try_write(request).ok().filter(|&count| count != 0)
        });
        let Some(Some(sent)) = sent else {
            return Outcome::NotSubmitted;
        };

        let exchange = async {
            stream.write_all(&request[sent..]).await?;
            read_reply(&stream).await
        };
        tokio::select! {
            biased;
            () = context.ct.cancelled() => Outcome::Uncertain,
            result = timeout_at(deadline, exchange) => match result {
                Ok(Ok(reply)) => reply,
                _ => Outcome::Uncertain,
            },
        }
    }
}

impl ServerHandler for Handler {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_protocol_version(ProtocolVersion::V_2025_11_25)
            .with_instructions(
                "Development shim. Governed effects require verified M5a foundations.",
            )
    }

    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        static SUPPORTED: [ProtocolVersion; 1] = [ProtocolVersion::V_2025_11_25];
        Cow::Borrowed(&SUPPORTED)
    }

    async fn list_tools(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        if !self.admitted(&context) {
            return Err(not_admitted());
        }
        if request.is_some_and(|params| params.cursor.is_some()) {
            return Err(ErrorData::invalid_params("Unknown tool list cursor", None));
        }
        let mut result = ListToolsResult::default();
        if self.endpoint.is_some() {
            result.tools.push(tool());
        }
        Ok(result)
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        #[cfg(test)]
        self.before_call.run().await;

        if !self.admitted(&context) {
            return Err(not_admitted());
        }
        let request = proposal(request)?;
        let outcome = match &self.endpoint {
            Some(endpoint) => self.forward(endpoint, &request, &context).await,
            None => Outcome::Disabled,
        };
        Ok(outcome.into_result().into())
    }
}

fn not_admitted() -> ErrorData {
    ErrorData::invalid_request("MCP request was not admitted", None)
}

fn tool() -> Tool {
    let mut fields = JsonObject::new();
    for name in FIELDS {
        let field: JsonObject = [
            ("type".into(), "string".into()),
            ("minLength".into(), 1.into()),
        ]
        .into_iter()
        .collect();
        fields.insert(name.into(), field.into());
    }
    let mut schema = JsonObject::new();
    schema.insert("type".into(), "object".into());
    schema.insert("properties".into(), fields.into());
    schema.insert("required".into(), FIELDS.to_vec().into());
    schema.insert("additionalProperties".into(), false.into());
    Tool::new(TOOL, "Propose a create-only write from a snapshot.", schema)
}

fn proposal(request: CallToolRequestParams) -> Result<Vec<u8>, ErrorData> {
    let invalid = || ErrorData::invalid_params("Invalid create-only snapshot proposal", None);
    if request.name != TOOL {
        return Err(ErrorData::invalid_params("Unknown tool", None));
    }
    if request.input_responses.is_some() || request.request_state.is_some() {
        return Err(invalid());
    }
    let args = request.arguments.ok_or_else(invalid)?;
    if args.len() != FIELDS.len() {
        return Err(invalid());
    }
    let [Some(target), Some(source), Some(snapshot), Some(manifest), Some(content), Some(key)] =
        FIELDS.map(|name| args.get(name).and_then(|value| value.as_str()))
    else {
        return Err(invalid());
    };
    if !relative_path(target)
        || !relative_path(source)
        || !source.bytes().all(|byte| (0x20..=0x7e).contains(&byte))
        || !snapshot.strip_prefix("snap-").is_some_and(lower_hex_64)
        || !ids::is_digest(manifest)
        || !ids::is_digest(content)
        || key.is_empty()
        || key.len() > KEY_LIMIT
        || !key.bytes().all(|byte| byte.is_ascii_graphic())
    {
        return Err(invalid());
    }

    let mut fields = wire::Map::from([
        ("op".into(), wire::s("effect.request")),
        ("action".into(), wire::s(TOOL)),
        ("mode".into(), wire::s("create_only")),
    ]);
    for (name, value) in FIELDS
        .into_iter()
        .zip([target, source, snapshot, manifest, content, key])
    {
        fields.insert(name.into(), wire::s(value));
    }
    let canonical = wire::canonical(&wire::Value::Obj(fields)).map_err(|_| invalid())?;
    frame::encode(FrameType::Control, canonical.as_bytes()).ok_or_else(invalid)
}

fn lower_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn relative_path(path: &str) -> bool {
    path.len() <= PATH_LIMIT
        && !path.contains('\\')
        && !path.chars().any(char::is_control)
        && path.split('/').count() <= 64
        && path
            .split('/')
            .all(|component| !matches!(component, "" | "." | "..") && component.len() <= 255)
}

enum Outcome {
    Disabled,
    NotSubmitted,
    Uncertain,
    Refused(String),
    Receipt(String),
}

impl Outcome {
    fn into_result(self) -> CallToolResult {
        let message = match self {
            Self::Disabled => "Governed activation is unavailable.".into(),
            Self::NotSubmitted => "Writ request was not submitted.".into(),
            Self::Uncertain => {
                "Writ submission outcome is uncertain. Check the receipt before retrying.".into()
            }
            Self::Refused(code) => format!("Writ refused the proposal: {code}."),
            Self::Receipt(id) => {
                let object: JsonObject = [("receipt_id".into(), id.into())].into_iter().collect();
                return CallToolResult::structured(object.into());
            }
        };
        CallToolResult::error(vec![ContentBlock::text(message)])
    }
}

async fn read_reply(stream: &UnixStream) -> io::Result<Outcome> {
    let mut header = [0u8; frame::HEADER_BYTES];
    receive_exact(stream, &mut header).await?;
    let len = u32::from_be_bytes([header[0], header[1], header[2], header[3]]);
    let invalid = || io::Error::from(io::ErrorKind::InvalidData);
    if header[4] != FrameType::Control.byte() || len > frame::MAX_CONTROL_BYTES {
        return Err(invalid());
    }
    let mut bytes = header.to_vec();
    bytes.resize(frame::HEADER_BYTES + len as usize, 0);
    receive_exact(stream, &mut bytes[frame::HEADER_BYTES..]).await?;
    let Ok(Frame::Control(wire::Value::Obj(fields))) =
        frame::read_frame(&mut bytes.as_slice(), &FrameLimits::admin())
    else {
        return Err(invalid());
    };
    if fields.len() != 2 {
        return Err(invalid());
    }
    match fields.get("ok") {
        Some(wire::Value::Bool(true)) => fields
            .get("receipt_id")
            .and_then(wire::Value::as_str)
            .filter(|id| ids::is_id(id))
            .map(|id| Outcome::Receipt(id.into()))
            .ok_or_else(invalid),
        Some(wire::Value::Bool(false)) => fields
            .get("error")
            .and_then(wire::Value::as_str)
            .filter(|code| code.len() <= 64 && ids::is_reason_code(code))
            .map(|code| Outcome::Refused(code.into()))
            .ok_or_else(invalid),
        _ => Err(invalid()),
    }
}

async fn receive_exact(stream: &UnixStream, mut bytes: &mut [u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        let read = stream
            .async_io(tokio::io::Interest::READABLE, || recv_plain(stream, bytes))
            .await?;
        if read == 0 {
            return Err(io::Error::from(io::ErrorKind::UnexpectedEof));
        }
        bytes = &mut bytes[read..];
    }
    Ok(())
}
