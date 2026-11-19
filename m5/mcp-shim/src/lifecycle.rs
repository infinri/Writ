use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use rmcp::model::{
    ClientJsonRpcMessage, ClientNotification, ClientRequest, ErrorData, GetMeta, NumberOrString,
    ProtocolVersion, RequestId, RequestMetaObject, ServerJsonRpcMessage, ServerResult,
};
use rmcp::{transport::Transport, RoleServer};

pub(crate) const PROTOCOL: ProtocolVersion = ProtocolVersion::V_2025_11_25;
const MAX_TRACKED_IDS: usize = 128;
const MAX_ID_BYTES: usize = 256;

#[derive(Clone, Default)]
pub(crate) struct Admission(Arc<Mutex<State>>);

impl Admission {
    pub(crate) fn allowed(&self, id: &RequestId) -> bool {
        self.0.lock().is_ok_and(|state| {
            state.phase == Phase::Ready
                && matches!(
                    state.requests.get(id),
                    Some(RequestState::List | RequestState::Call | RequestState::Submitted)
                )
        })
    }

    // The first nonblocking socket write runs under the same lock that revokes admission
    // on cancellation and EOF. Claim each request once, including a failed write attempt.
    pub(crate) fn with_submission<T>(&self, id: &RequestId, f: impl FnOnce() -> T) -> Option<T> {
        let mut state = self.0.lock().ok()?;
        if state.phase != Phase::Ready || state.requests.get(id) != Some(&RequestState::Call) {
            return None;
        }
        state.requests.insert(id.clone(), RequestState::Submitted);
        Some(f())
    }

    pub(crate) fn close(&self) {
        if let Ok(mut state) = self.0.lock() {
            state.close();
        }
    }

    fn is_closed(&self) -> bool {
        self.0
            .lock()
            .map_or(true, |state| state.phase == Phase::Closed)
    }
}

#[derive(Default)]
struct State {
    phase: Phase,
    requests: HashMap<RequestId, RequestState>,
}

#[derive(Default, PartialEq, Eq)]
enum Phase {
    #[default]
    Fresh,
    AwaitingResponse(RequestId),
    AwaitingInitialized,
    Ready,
    Closed,
}

#[derive(PartialEq, Eq)]
enum RequestState {
    Other,
    Denied,
    List,
    Call,
    Submitted,
    Cancelled,
}

enum Incoming {
    Forward,
    Refuse(RequestId, ErrorData),
    Close,
}

enum Sent {
    Initialized(RequestId),
    Response(RequestId),
    Other,
}

impl State {
    fn close(&mut self) {
        self.phase = Phase::Closed;
        self.requests.clear();
    }

    fn incoming(&mut self, message: &ClientJsonRpcMessage) -> Incoming {
        match message {
            ClientJsonRpcMessage::Request(request) => {
                let id = &request.id;
                if self.requests.contains_key(id)
                    || self.requests.len() >= MAX_TRACKED_IDS
                    || matches!(id, NumberOrString::String(id) if id.len() > MAX_ID_BYTES)
                {
                    // rmcp maps both callbacks and replies by ID. A duplicate cannot be
                    // answered by ID without a chance of binding it to the original.
                    return Incoming::Close;
                }
                self.request(id, &request.request)
            }
            ClientJsonRpcMessage::Notification(notification) => {
                self.notification(&notification.notification)
            }
            _ if self.phase == Phase::Fresh => Incoming::Close,
            _ => Incoming::Forward,
        }
    }

    fn request(&mut self, id: &RequestId, request: &ClientRequest) -> Incoming {
        if request.method() == "initialize" {
            return if self.phase == Phase::Fresh
                && matches!(request, ClientRequest::InitializeRequest(_))
            {
                self.phase = Phase::AwaitingResponse(id.clone());
                Incoming::Forward
            } else {
                Incoming::Close
            };
        }
        let meta = request.get_meta();
        if meta.contains_key(RequestMetaObject::DRAFT_REQUIRED_KEYS[0]) {
            match meta.protocol_version() {
                Some(version) if version == PROTOCOL => {}
                Some(version) => {
                    return Incoming::Refuse(
                        id.clone(),
                        ErrorData::unsupported_protocol_version(version, &[PROTOCOL]),
                    );
                }
                None => {
                    return Incoming::Refuse(
                        id.clone(),
                        ErrorData::invalid_request("Invalid inline MCP version", None),
                    );
                }
            }
        }
        match self.phase {
            Phase::Fresh if matches!(request, ClientRequest::PingRequest(_)) => {
                return Incoming::Forward;
            }
            Phase::Fresh => {
                // Do not let rmcp select its alternative stateless lifecycle.
                return Incoming::Refuse(
                    id.clone(),
                    ErrorData::invalid_request("MCP initialization required", None),
                );
            }
            Phase::AwaitingResponse(_) | Phase::Closed => return Incoming::Close,
            Phase::AwaitingInitialized | Phase::Ready => {}
        }
        let ready = self.phase == Phase::Ready;
        let state = match request {
            ClientRequest::ListToolsRequest(_) if ready => RequestState::List,
            ClientRequest::CallToolRequest(_) if ready => RequestState::Call,
            ClientRequest::ListToolsRequest(_) | ClientRequest::CallToolRequest(_) => {
                RequestState::Denied
            }
            _ => RequestState::Other,
        };
        self.requests.insert(id.clone(), state);
        Incoming::Forward
    }

    fn notification(&mut self, notification: &ClientNotification) -> Incoming {
        match notification {
            ClientNotification::InitializedNotification(_) => {
                if self.phase != Phase::AwaitingInitialized {
                    return Incoming::Close;
                }
                self.phase = Phase::Ready;
            }
            ClientNotification::CustomNotification(notification)
                if notification.method == "notifications/initialized" =>
            {
                return Incoming::Close;
            }
            ClientNotification::CancelledNotification(notification) => {
                if let Some(id) = &notification.params.request_id {
                    if let Some(request) = self.requests.get_mut(id) {
                        // rmcp drops the response of a canceled request. Retain this slot
                        // because its callback may still run and has the same request ID.
                        *request = RequestState::Cancelled;
                    }
                }
            }
            _ => {}
        }
        Incoming::Forward
    }

    fn outgoing(&mut self, message: &ServerJsonRpcMessage) -> io::Result<Sent> {
        match message {
            ServerJsonRpcMessage::Response(response) => {
                if let ServerResult::InitializeResult(result) = &response.result {
                    if self.phase == Phase::AwaitingResponse(response.id.clone())
                        && result.protocol_version == PROTOCOL
                    {
                        return Ok(Sent::Initialized(response.id.clone()));
                    }
                    return Err(io::Error::other("Unexpected MCP initialize response"));
                }
                if matches!(self.phase, Phase::AwaitingResponse(_)) {
                    return Err(io::Error::other("Expected MCP initialize response"));
                }
                Ok(Sent::Response(response.id.clone()))
            }
            ServerJsonRpcMessage::Error(error) => {
                if matches!(self.phase, Phase::AwaitingResponse(_)) {
                    self.close();
                }
                Ok(error.id.clone().map_or(Sent::Other, Sent::Response))
            }
            _ => Ok(Sent::Other),
        }
    }

    fn sent(&mut self, sent: Sent) {
        match sent {
            Sent::Initialized(id) if self.phase == Phase::AwaitingResponse(id.clone()) => {
                self.phase = Phase::AwaitingInitialized;
            }
            Sent::Response(id) if self.requests.get(&id) != Some(&RequestState::Cancelled) => {
                self.requests.remove(&id);
            }
            _ => {}
        }
    }
}

type Refusal = Pin<Box<dyn Future<Output = io::Result<()>> + Send>>;

pub(crate) struct Ordered<T> {
    inner: T,
    admission: Admission,
    refusal: Option<Refusal>,
}

impl<T> Ordered<T> {
    pub(crate) fn new(inner: T, admission: Admission) -> Self {
        Self {
            inner,
            admission,
            refusal: None,
        }
    }
}

impl<T> Drop for Ordered<T> {
    fn drop(&mut self) {
        self.admission.close();
    }
}

impl<T: Transport<RoleServer>> Transport<RoleServer> for Ordered<T> {
    type Error = io::Error;

    fn send(
        &mut self,
        item: ServerJsonRpcMessage,
    ) -> impl Future<Output = io::Result<()>> + Send + 'static {
        let admission = self.admission.clone();
        let sent = admission.0.lock().map_or_else(
            |_| Err(io::Error::other("MCP admission unavailable")),
            |mut state| {
                let result = state.outgoing(&item);
                if result.is_err() {
                    state.close();
                }
                result
            },
        );
        let write = sent.map(|sent| (sent, self.inner.send(item)));
        async move {
            let result = match write {
                Ok((sent, write)) => write.await.map(|()| sent).map_err(io::Error::other),
                Err(error) => Err(error),
            };
            match result {
                Ok(sent) => {
                    let mut state = admission
                        .0
                        .lock()
                        .map_err(|_| io::Error::other("MCP admission unavailable"))?;
                    state.sent(sent);
                    Ok(())
                }
                Err(error) => {
                    admission.close();
                    Err(error)
                }
            }
        }
    }

    async fn receive(&mut self) -> Option<ClientJsonRpcMessage> {
        while !self.admission.is_closed() {
            if let Some(write) = &mut self.refusal {
                let result = write.await;
                self.refusal = None;
                if result.is_err() {
                    break;
                }
            }
            let Some(message) = self.inner.receive().await else {
                break;
            };
            let decision = self
                .admission
                .0
                .lock()
                .map_or(Incoming::Close, |mut state| {
                    if state.phase == Phase::Closed {
                        return Incoming::Close;
                    }
                    let decision = state.incoming(&message);
                    if matches!(decision, Incoming::Close) {
                        state.close();
                    }
                    decision
                });
            match decision {
                Incoming::Forward => return Some(message),
                Incoming::Close => break,
                Incoming::Refuse(id, error) => {
                    // rmcp can drop and repoll this receive future when a handler
                    // finishes. Keep an interrupted refusal write on the transport.
                    self.refusal = Some(Box::pin(
                        self.send(ServerJsonRpcMessage::error(error, Some(id))),
                    ));
                }
            }
        }
        self.admission.close();
        None
    }

    async fn close(&mut self) -> io::Result<()> {
        self.admission.close();
        self.inner.close().await.map_err(io::Error::other)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::collections::VecDeque;

    use rmcp::model::{
        CallToolRequest, CallToolRequestParams, CancelledNotification, CancelledNotificationParam,
        Implementation, InitializeRequest, InitializeRequestParams, InitializeResult,
        InitializedNotification, ListToolsRequest, ServerCapabilities,
    };

    use super::*;

    #[derive(Default)]
    struct TypedTransport {
        incoming: VecDeque<ClientJsonRpcMessage>,
        sent: Arc<Mutex<Vec<ServerJsonRpcMessage>>>,
    }

    impl Transport<RoleServer> for TypedTransport {
        type Error = io::Error;

        fn send(
            &mut self,
            message: ServerJsonRpcMessage,
        ) -> impl Future<Output = io::Result<()>> + Send + 'static {
            let sent = self.sent.clone();
            async move {
                sent.lock().unwrap().push(message);
                Ok(())
            }
        }

        async fn receive(&mut self) -> Option<ClientJsonRpcMessage> {
            self.incoming.pop_front()
        }

        async fn close(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn call(id: i64) -> ClientJsonRpcMessage {
        ClientJsonRpcMessage::request(
            CallToolRequest::new(CallToolRequestParams::new("test")).into(),
            RequestId::Number(id),
        )
    }

    fn initialize() -> ClientJsonRpcMessage {
        ClientJsonRpcMessage::request(
            InitializeRequest::new(InitializeRequestParams::new(
                Default::default(),
                Implementation::from_build_env(),
            ))
            .into(),
            RequestId::Number(1),
        )
    }

    fn initialized() -> ClientJsonRpcMessage {
        ClientJsonRpcMessage::notification(InitializedNotification::default().into())
    }

    fn initialize_response(version: ProtocolVersion) -> ServerJsonRpcMessage {
        ServerJsonRpcMessage::response(
            InitializeResult::new(ServerCapabilities::default())
                .with_protocol_version(version)
                .into(),
            RequestId::Number(1),
        )
    }

    fn run(test: impl Future<Output = ()>) {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(test);
    }

    #[test]
    fn admission_is_frozen_on_receive_and_submission_is_single_use() {
        run(async {
            let admission = Admission::default();
            let inner = TypedTransport {
                incoming: [call(10), initialize(), call(11), initialized(), call(12)].into(),
                ..Default::default()
            };
            let sent = inner.sent.clone();
            let mut ordered = Ordered::new(inner, admission.clone());

            assert_eq!(
                ordered.receive().await.unwrap().into_request().unwrap().1,
                RequestId::Number(1)
            );
            assert!(matches!(
                sent.lock().unwrap().as_slice(),
                [ServerJsonRpcMessage::Error(error)] if error.id == Some(RequestId::Number(10))
            ));
            ordered.send(initialize_response(PROTOCOL)).await.unwrap();
            let (_, early) = ordered.receive().await.unwrap().into_request().unwrap();
            assert!(ordered
                .receive()
                .await
                .unwrap()
                .into_notification()
                .is_some());
            let (_, ready) = ordered.receive().await.unwrap().into_request().unwrap();

            assert!(!admission.allowed(&early));
            assert_eq!(admission.with_submission(&early, || 42), None);
            assert!(admission.allowed(&ready));
            assert_eq!(admission.with_submission(&ready, || 42), Some(42));
            assert_eq!(admission.with_submission(&ready, || 42), None);
            assert!(ordered.receive().await.is_none());
            assert!(!admission.allowed(&ready));
        });
    }

    #[test]
    fn wrong_initialize_reply_closes_admission_before_forwarding() {
        run(async {
            let admission = Admission::default();
            let inner = TypedTransport {
                incoming: [initialize(), initialized(), call(2)].into(),
                ..Default::default()
            };
            let sent = inner.sent.clone();
            let mut ordered = Ordered::new(inner, admission.clone());

            assert!(ordered.receive().await.is_some());
            assert!(ordered
                .send(initialize_response(ProtocolVersion::V_2024_11_05))
                .await
                .is_err());
            assert!(sent.lock().unwrap().is_empty());
            assert!(ordered.receive().await.is_none());
            assert!(!admission.allowed(&RequestId::Number(2)));
        });
    }

    #[test]
    fn cancellation_revokes_pending_call_and_reused_id_closes_session() {
        run(async {
            let admission = Admission::default();
            let id = RequestId::Number(2);
            let cancel = ClientJsonRpcMessage::notification(
                CancelledNotification::new(CancelledNotificationParam::new(Some(id.clone()), None))
                    .into(),
            );
            let inner = TypedTransport {
                incoming: [initialize(), initialized(), call(2), cancel, call(2)].into(),
                ..Default::default()
            };
            let mut ordered = Ordered::new(inner, admission.clone());
            assert!(ordered.receive().await.is_some());
            ordered.send(initialize_response(PROTOCOL)).await.unwrap();
            assert!(ordered.receive().await.is_some());
            assert!(ordered.receive().await.is_some());
            assert!(admission.allowed(&id));

            assert!(ordered.receive().await.is_some());
            assert!(!admission.allowed(&id));
            assert_eq!(admission.with_submission(&id, || 42), None);
            assert!(ordered.receive().await.is_none());
        });
    }

    #[test]
    fn duplicate_lifecycle_or_in_flight_id_revokes_pending_call() {
        for duplicate in [initialize(), initialized(), call(2)] {
            run(async {
                let admission = Admission::default();
                let id = RequestId::Number(2);
                let inner = TypedTransport {
                    incoming: [initialize(), initialized(), call(2), duplicate].into(),
                    ..Default::default()
                };
                let mut ordered = Ordered::new(inner, admission.clone());
                assert!(ordered.receive().await.is_some());
                ordered.send(initialize_response(PROTOCOL)).await.unwrap();
                assert!(ordered.receive().await.is_some());
                assert!(ordered.receive().await.is_some());
                assert!(admission.allowed(&id));
                assert!(ordered.receive().await.is_none());
                assert_eq!(admission.with_submission(&id, || 42), None);
            });
        }
    }

    #[test]
    fn list_admission_never_grants_submission() {
        run(async {
            let admission = Admission::default();
            let id = RequestId::Number(2);
            let list =
                ClientJsonRpcMessage::request(ListToolsRequest::default().into(), id.clone());
            let inner = TypedTransport {
                incoming: [initialize(), initialized(), list].into(),
                ..Default::default()
            };
            let mut ordered = Ordered::new(inner, admission.clone());
            assert!(ordered.receive().await.is_some());
            ordered.send(initialize_response(PROTOCOL)).await.unwrap();
            assert!(ordered.receive().await.is_some());
            assert!(ordered.receive().await.is_some());
            assert!(admission.allowed(&id));
            assert_eq!(admission.with_submission(&id, || 42), None);
        });
    }
}
